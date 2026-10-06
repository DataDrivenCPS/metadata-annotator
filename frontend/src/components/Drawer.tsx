import { useEffect, useState } from 'react'
import { api } from '../api'
import { useStore } from '../store'
import { emptySelection, type EntityDetail, type EntityRelations, type Revision, type ReviewIssue } from '../types'
import { CandidateOptions } from './cells'
import { DismissalNote, RepairDetail } from './IssuesView'
import { shortIri } from './TermPicker'

export function Drawer() {
  const inspectId = useStore((s) => s.inspectId)
  const tab = useStore((s) => s.drawerTab)
  const setDrawerTab = useStore((s) => s.setDrawerTab)

  // Inspecting an object shows its details, unless its RDF is already open.
  useEffect(() => {
    if (inspectId && useStore.getState().drawerTab !== 'rdf') setDrawerTab('inspector')
  }, [inspectId, setDrawerTab])

  return (
    <section className="drawer">
      <nav className="tabs small">
        <button className={tab === 'inspector' ? 'active' : ''} onClick={() => setDrawerTab('inspector')}>Inspector</button>
        <button className={tab === 'rdf' ? 'active' : ''} title="The inspected object's RDF, as Turtle"
          onClick={() => setDrawerTab('rdf')}>RDF</button>
        <button className={tab === 'history' ? 'active' : ''} onClick={() => setDrawerTab('history')}>History</button>
      </nav>
      <div className="drawer-body">
        {tab === 'inspector' && <Inspector />}
        {tab === 'rdf' && <RdfView />}
        {tab === 'history' && <History />}
      </div>
    </section>
  )
}

function useEntityDetail() {
  const projectId = useStore((s) => s.projectId)!
  const head = useStore((s) => s.model?.head)
  const viewing = useStore((s) => s.viewing)
  const inspectId = useStore((s) => s.inspectId)
  const [detail, setDetail] = useState<EntityDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [version, setVersion] = useState(0)

  useEffect(() => {
    if (!inspectId) { setDetail(null); return }
    setError(null)
    api.entity(projectId, inspectId, viewing ?? undefined).then(setDetail).catch((e) => { setDetail(null); setError(String(e.message ?? e)) })
  }, [projectId, inspectId, head, viewing, version])

  return { inspectId, detail, error, refresh: () => setVersion((v) => v + 1) }
}

function Inspector() {
  const projectId = useStore((s) => s.projectId)!
  const setSelection = useStore((s) => s.setSelection)
  const appendAssistantContext = useStore((s) => s.appendAssistantContext)
  const notify = useStore((s) => s.notify)
  const reload = useStore((s) => s.reload)
  const repairs = useStore((s) => s.repairs?.byIssue)
  const { inspectId, detail, error, refresh } = useEntityDetail()

  if (!inspectId) return <p className="muted pad">Select an object to inspect its ontology terms, evidence, and history.</p>
  if (error) return <p className="error-text pad">{error}</p>
  if (!detail) return <p className="muted pad">Loading…</p>
  return (
    <div className="inspector">
      <h4>{detail.row?.label ?? detail.id}</h4>
      <dl>
        <dt>Id</dt><dd><code>{detail.id}</code></dd>
        <dt>IRI</dt><dd><code>{detail.iri}</code></dd>
        <dt>Types</dt><dd>{detail.types.map((t) => <div key={t.iri} title={t.iri}>{t.label} <code>{shortIri(t.iri)}</code></div>)}</dd>
        <dt>Set or confirmed by a person</dt><dd>{detail.locked.length ? detail.locked.join(', ') : <span className="muted">—</span>}</dd>
      </dl>
      <h5>Issues on this object</h5>
      {detail.issues.length ? (
        <ul className="inspector-issues">
          {detail.issues.map((issue) => (
            <li key={issue.id} className={`inspector-issue sev-${issue.severity}`}>
              <div className="inspector-issue-heading">
                <span className={`sev ${issue.severity}`}>{issue.severity}</span>
                <span>{issue.category.replaceAll('_', ' ')}</span>
                <span className="issue-actions">
                  {issue.resolution_state === 'dismissed' && <span className="muted">dismissed</span>}
                  <button className="link"
                    title={issue.resolution_state === 'dismissed' ? 'Show this issue as open again' : 'Hide this issue from the open list'}
                    onClick={() => void api.setIssueState(projectId, issue.id,
                      issue.resolution_state === 'dismissed' ? 'open' : 'dismissed')
                      .then(() => Promise.all([reload(), refresh()]))
                      .catch((e) => notify({ kind: 'error', text: String(e.message ?? e) }))}>
                    {issue.resolution_state === 'dismissed' ? 'Reopen' : 'Dismiss'}
                  </button>
                  <button className="link" onClick={() => {
                    const isConnection = detail.row?.kind === 'connection'
                    setSelection({ ...emptySelection(),
                      entity_ids: isConnection ? [] : [detail.id],
                      relationship_ids: isConnection ? [detail.id] : [] })
                    appendAssistantContext(issuePrompt(detail, issue))
                    notify({ kind: 'info', text: 'Issue context is in the assistant prompt. Review before sending.' })
                  }}>Use in chat</button>
                </span>
              </div>
              <p>{issue.explanation}</p>
              {issue.dismissal && <DismissalNote dismissal={issue.dismissal} />}
              {repairs?.[issue.id] && <RepairDetail repair={repairs[issue.id]} />}
              {issue.details.findings?.map((finding, index) => (
                <div className="finding-detail" key={`${finding.focus}-${index}`}>
                  {finding.message && <p>{finding.message}</p>}
                  <dl>
                    {finding.focus && <><dt>Focus</dt><dd><code>{finding.focus}</code></dd></>}
                    {finding.path && <><dt>Path</dt><dd><code>{finding.path}</code></dd></>}
                    {finding.shape && <><dt>Shape</dt><dd><code>{finding.shape}</code></dd></>}
                  </dl>
                </div>
              ))}
            </li>
          ))}
        </ul>
      ) : <p className="muted">No issues recorded for this object.</p>}
      <RelationsPanel eid={detail.id} />
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
  )
}

/** The entity's ontology relations, and adding one the vocabulary allows (from its shapes). */
function RelationsPanel({ eid }: { eid: string }) {
  const projectId = useStore((s) => s.projectId)!
  const head = useStore((s) => s.model?.head)
  const viewing = useStore((s) => s.viewing)
  const edit = useStore((s) => s.edit)
  const [data, setData] = useState<EntityRelations | null>(null)
  const [adding, setAdding] = useState<{ relation: string; filter: string } | null>(null)

  useEffect(() => {
    let alive = true
    api.entityRelations(projectId, eid, viewing ?? undefined).then((d) => alive && setData(d)).catch(() => alive && setData(null))
    return () => { alive = false }
  }, [projectId, eid, head, viewing])

  if (!data) return null
  const chosen = adding ? data.allowed.find((a) => a.iri === adding.relation) : undefined
  const shown = data.allowed.filter((a) => !adding?.filter || `${a.label} ${a.curie}`.toLowerCase().includes(adding.filter.toLowerCase()))
  return <>
    <h5>Relationships</h5>
    {data.relationships.length ? <ul className="relations">
      {data.relationships.map((r) => {
        const outgoing = r.subject.id === eid
        const other = outgoing ? (r.object?.label ?? r.value?.label ?? '?') : r.subject.label
        return <li key={r.id}>
          {outgoing ? <><span className="rel-name">{r.relation.label}</span> → {other}</>
            : <>{other} <span className="rel-name">{r.relation.label}</span> → this</>}
          {!viewing && <button className="link" title="Remove this relationship"
            onClick={() => void edit([{ op: 'unrelate', id: r.id }])}>×</button>}
        </li>
      })}
    </ul> : <p className="muted">No other relationships.</p>}
    {!viewing && data.allowed.length > 0 && (adding === null
      ? <button className="link" onClick={() => setAdding({ relation: '', filter: '' })}>+ Add relationship</button>
      : <div className="relation-add">
        {!chosen ? <>
          <input autoFocus placeholder={`Filter ${data.allowed.length} relations the vocabulary allows…`} value={adding.filter}
            onChange={(e) => setAdding({ ...adding, filter: e.target.value })} />
          <ul className="relation-choices">{shown.slice(0, 40).map((a) => <li key={a.iri}>
            <button className="link" onClick={() => setAdding({ ...adding, relation: a.iri })}
              title={a.objects.length ? `Points to: ${a.objects.map((o) => o.curie).join(', ')}` : 'Unconstrained'}>
              {a.label} <code>{a.curie}</code></button>
            <span className="muted small"> → {a.objects.map((o) => o.label).join(' / ') || 'anything'}{a.max === 1 ? ' (one)' : ''}
              {a.typed_field ? ` · also shown as ${a.typed_field}` : ''}</span>
          </li>)}</ul>
        </> : <>
          <div><span className="rel-name">{chosen.label}</span> → {chosen.objects.map((o) => o.label).join(' / ') || 'anything'}</div>
          {chosen.candidates.length ? <select autoFocus defaultValue="" onChange={(e) => {
            const c = chosen.candidates[Number(e.target.value)]
            if (!c) return
            setAdding(null)
            void edit([{ op: 'relate', subject: eid, relation: chosen.iri, object: c.id ?? c.curie ?? c.iri }])
          }}>
            <option value="" disabled>choose…</option>
            <CandidateOptions candidates={chosen.candidates}
              describe={(c) => `${c.label}${c.kind === 'value' ? ` (${c.curie})` : ` · ${(c.kind ?? '').replace('_', ' ')}`}`} />
          </select> : <p className="muted small">Nothing else in the model to relate yet; create it first (or ask the assistant).</p>}
        </>}
        <button className="link" onClick={() => setAdding(null)}>cancel</button>
      </div>)}
  </>
}

function RdfView() {
  const { inspectId, detail, error } = useEntityDetail()

  if (!inspectId) return <p className="muted pad">Select an object to see its RDF.</p>
  if (error) return <p className="error-text pad">{error}</p>
  if (!detail) return <p className="muted pad">Loading…</p>
  return (
    <div className="inspector">
      <h4>{detail.row?.label ?? detail.id} <span className="muted small">Turtle</span></h4>
      <pre className="code turtle">{detail.turtle}</pre>
    </div>
  )
}

function issuePrompt(detail: EntityDetail, issue: ReviewIssue): string {
  const label = detail.row?.label ?? detail.id
  const findings = issue.details.findings?.map((finding, index) => [
    `Finding ${index + 1}: ${finding.message}`,
    finding.focus ? `Focus: ${finding.focus}` : '',
    finding.path ? `Path: ${finding.path}` : '',
    finding.shape ? `Shape: ${finding.shape}` : '',
  ].filter(Boolean).join('\n')) ?? []
  return [
    'Please review this issue in the current model and propose a correction if it still applies. Check the related context and explain your reasoning.',
    `Issue [${issue.id}] (${issue.severity}, ${issue.category.replaceAll('_', ' ')}): ${issue.explanation}`,
    `Affected object: ${label} (${detail.row?.kind ?? 'entity'})`,
    `Object ID: ${detail.id}`,
    `Object IRI: ${detail.iri}`,
    detail.types.length ? `Ontology types: ${detail.types.map((type) => `${type.label} (${type.iri})`).join('; ')}` : '',
    findings.length ? `Validation details:\n${findings.join('\n\n')}` : '',
  ].filter(Boolean).join('\n\n')
}

const fmtJson = (s: string) => { try { const v = JSON.parse(s); return v === null ? '—' : String(v) } catch { return s } }

function History() {
  const projectId = useStore((s) => s.projectId)!
  const head = useStore((s) => s.model?.head)
  const shown = useStore((s) => s.model?.revision.id)
  const viewRevision = useStore((s) => s.viewRevision)
  const [revs, setRevs] = useState<Revision[]>([])
  useEffect(() => { api.revisions(projectId).then(setRevs) }, [projectId, head])
  return (
    <ul className="revisions">
      {revs.map((r) => (
        <li key={r.id} className={`${r.id === head ? 'current' : ''} ${r.id === shown ? 'shown' : ''}`}>
          <code>{r.id}</code> <span className={`author ${r.author}`}>{r.author}</span> {r.summary}
          <span className="muted"> · {new Date(r.created_at).toLocaleString()}
            {r.validation ? ` · ${r.validation.violations} problem(s)` : ''}</span>
          {r.id === head && <span className="badge">current</span>}
          {r.id === shown && r.id !== head && <span className="badge">viewing</span>}
          <span className="revision-actions">
            {r.id !== shown && <button className="link" onClick={() => void viewRevision(r.id === head ? null : r.id)}
              title={r.id === head ? 'Back to the current model' : 'Browse this revision read-only'}>{r.id === head ? 'back to current' : 'view'}</button>}
            <a href={api.exportUrl(projectId, r.id, 'ttl')} download title={`Download the model at ${r.id} as Turtle`}>.ttl</a>
          </span>
        </li>
      ))}
    </ul>
  )
}
