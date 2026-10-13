import { useEffect, useState } from 'react'
import { api } from '../api'
import { useStore } from '../store'
import { type EntityDetail, type EntityPropertySpec, type EntityPropertyValue, type EntityRelations, type Revision } from '../types'
import { CandidateOptions } from './cells'
import { DismissalNote, IssueDetails } from './IssuesView'
import { shortIri, TermPicker } from './TermPicker'

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
    let alive = true
    setDetail(null)
    setError(null)
    if (!inspectId || isTermId(inspectId)) return
    api.entity(projectId, inspectId, viewing ?? undefined)
      .then((detail) => { if (alive) setDetail(detail) })
      .catch((e) => { if (alive) setError(String(e.message ?? e)) })
    return () => { alive = false }
  }, [projectId, inspectId, head, viewing, version])

  return { inspectId, detail, error, refresh: () => setVersion((v) => v + 1) }
}

/** Rows of a vocabulary view (e.g. Processes) are terms, not model objects: their id is the IRI. */
const isTermId = (id: string) => id.includes('://') || id.startsWith('urn:')

/** A vocabulary term: what it is called, its kind and description. */
function TermInspector({ iri }: { iri: string }) {
  const profile = useStore((s) => s.model?.info.profile)
  const [term, setTerm] = useState<Awaited<ReturnType<typeof api.term>> | null>(null)
  useEffect(() => {
    if (!profile) return
    let alive = true
    api.term(iri, profile).then((t) => alive && setTerm(t)).catch(() => alive && setTerm(null))
    return () => { alive = false }
  }, [iri, profile])
  if (!term) return <p className="muted pad">{shortIri(iri)}</p>
  return <div className="inspector">
    <h4>{term.label}</h4>
    <dl>
      <dt>Term</dt><dd><code>{shortIri(iri)}</code> · vocabulary {term.kind?.replace('_', ' ')}</dd>
      {term.comment && <><dt>About</dt><dd>{term.comment}</dd></>}
      {term.deprecated && <><dt>Deprecated</dt><dd>{term.replaced_by ? <>use <code>{shortIri(term.replaced_by)}</code></> : 'yes'}</dd></>}
    </dl>
  </div>
}

/** Locked fields are ones a person set: automatic fixes leave them alone (the assistant can still be asked to change them). */
function LocksEditor({ eid, locked }: { eid: string; locked: string[] }) {
  const viewing = useStore((s) => s.viewing)
  const edit = useStore((s) => s.edit)
  return <>
    {locked.length ? locked.map((f) => <span key={f} className="lock-chip">{f}
      {!viewing && <button className="link" title={`Unlock ${f}: automatic fixes may change it`}
        onClick={() => void edit([{ op: 'set_lock', id: eid, locked: false, fields: [f] }])}>×</button>}
    </span>) : <span className="muted">—</span>}
    {!viewing && <div>
      <button className="link" title="Keep automatic fixes from changing any of this row's fields"
        onClick={() => void edit([{ op: 'set_lock', id: eid, locked: true }])}>Lock all</button>
      {locked.length > 0 && <button className="link" title="Let automatic fixes change this row"
        onClick={() => void edit([{ op: 'set_lock', id: eid, locked: false }])}>Unlock all</button>}
    </div>}
  </>
}

/** An entity's classes: it can be an instance of several; add one or drop one (it keeps at least one). */
function TypesEditor({ eid, types }: { eid: string; types: { iri: string; label: string }[] }) {
  const viewing = useStore((s) => s.viewing)
  const edit = useStore((s) => s.edit)
  const [adding, setAdding] = useState(false)
  return <>
    {types.map((t) => <div key={t.iri} title={t.iri}>{t.label} <code>{shortIri(t.iri)}</code>
      {!viewing && types.length > 1 && <button className="link" title="Remove this type"
        onClick={() => void edit([{ op: 'remove_type', id: eid, type: t.iri }])}>×</button>}
    </div>)}
    {!viewing && (adding
      ? <TermPicker kind="class" allowClear={false} onCancel={() => setAdding(false)}
        onPick={(iri) => { setAdding(false); if (iri) void edit([{ op: 'add_type', id: eid, type: iri }]) }} />
      : <button className="link" onClick={() => setAdding(true)}>+ Add type</button>)}
  </>
}

function Inspector() {
  const projectId = useStore((s) => s.projectId)!
  const addIssuesToPrompt = useStore((s) => s.addIssuesToPrompt)
  const notify = useStore((s) => s.notify)
  const reload = useStore((s) => s.reload)
  const repairs = useStore((s) => s.repairs?.byIssue)
  const { inspectId, detail, error, refresh } = useEntityDetail()

  if (!inspectId) return <p className="muted pad">Select an object to inspect its ontology terms, evidence, and history.</p>
  if (isTermId(inspectId)) return <TermInspector iri={inspectId} />
  if (error) return <p className="error-text pad">{error}</p>
  if (!detail) return <p className="muted pad">Loading…</p>
  return (
    <div className="inspector">
      <h4>{detail.row?.label ?? detail.id}</h4>
      <dl>
        <dt>Id</dt><dd><code>{detail.id}</code></dd>
        <dt>IRI</dt><dd><code>{detail.iri}</code></dd>
        <dt>Types</dt><dd><TypesEditor eid={detail.id} types={detail.types} /></dd>
        <dt>Set or confirmed by a person</dt><dd><LocksEditor eid={detail.id} locked={detail.locked} /></dd>
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
                    addIssuesToPrompt([issue])
                    notify({ kind: 'info', text: 'Issue context is in the assistant prompt. Review before sending.' })
                  }}>Use in chat</button>
                </span>
              </div>
              <p>{issue.explanation}</p>
              {issue.dismissal && <DismissalNote dismissal={issue.dismissal} />}
              <IssueDetails issue={issue} repair={repairs?.[issue.id]} />
            </li>
          ))}
        </ul>
      ) : <p className="muted">No issues recorded for this object.</p>}
      <PropertiesPanel eid={detail.id} properties={detail.properties ?? []} />
      <RelationsPanel eid={detail.id} />
      <h5>Evidence</h5>
      {detail.evidence.length ? detail.evidence.map((e) => (
        <div key={e.id} className="evidence-item">
          <a href={`${api.sourceFileUrl(projectId, String(e.location.source_id))}${e.location.page ? `#page=${e.location.page}` : ''}`}
            target="_blank" rel="noreferrer">{String(e.content.filename ?? e.content.name ?? 'Source')}
            {e.location.page ? ` · page ${e.location.page}` : ''}
            {typeof e.location.row === 'number' ? ` · row ${e.location.row + 1}` : ''}
            {typeof e.location.column === 'number' ? ` · column ${e.location.column + 1}` : ''}
          </a>
          {Array.isArray(e.location.text_range) && <span className="muted"> · characters {e.location.text_range[0] + 1}–{e.location.text_range[1]}</span>}
          {typeof e.content.text === 'string' && <p className="evidence-excerpt">{e.content.text}</p>}
          {!!e.content.metadata && <p className="muted">{JSON.stringify(e.content.metadata)}</p>}
        </div>
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

/** Static entity properties (Brick: area, tilt, year built...): a value, with a unit where the property has one. */
function PropertiesPanel({ eid, properties }: { eid: string; properties: EntityPropertyValue[] }) {
  const brick = useStore((s) => s.model?.info.family === 'brick')
  const profile = useStore((s) => s.model?.info.profile ?? 'brick')
  const viewing = useStore((s) => s.viewing)
  const edit = useStore((s) => s.edit)
  const [editing, setEditing] = useState<{ iri: string; label: string; value: string; unit: string } | 'pick' | null>(null)
  const [loaded, setLoaded] = useState<{ iri: string; spec: EntityPropertySpec | null } | null>(null)
  const iri = editing && editing !== 'pick' ? editing.iri : null
  useEffect(() => {
    if (!iri) return
    let alive = true
    api.term(iri, profile)
      .then((t) => alive && setLoaded({ iri, spec: (t as { value_spec?: EntityPropertySpec }).value_spec ?? null }))
      .catch(() => alive && setLoaded({ iri, spec: null }))
    return () => { alive = false }
  }, [iri, profile])
  const ready = loaded !== null && loaded.iri === iri  // save waits for the property's value shape (its units)
  const spec = ready ? loaded.spec : null
  if (!brick) return null
  const save = () => {
    if (!editing || editing === 'pick' || !ready || !editing.iri || editing.value.trim() === '') return
    const unit = editing.unit || (spec?.unit_terms.length === 1 ? spec.unit_terms[0].iri : '')
    void edit([{ op: 'set_property', id: eid, property: editing.iri, value: editing.value.trim(), ...(unit ? { unit } : {}) }])
    setEditing(null)
  }
  return <>
    <h5>Properties</h5>
    {properties.length ? <ul className="relations">
      {properties.map((p) => <li key={p.property.iri ?? p.property.label}>
        <span className="rel-name">{p.property.label}</span> {p.value}{(p.unit_symbol || p.unit?.label) ? ` ${p.unit_symbol || p.unit?.label}` : ''}
        {!viewing && p.property.iri && <>
          <button className="link" title="Change this value"
            onClick={() => setEditing({ iri: p.property.iri ?? '', label: p.property.label, value: p.value ?? '', unit: p.unit?.iri ?? '' })}>edit</button>
          <button className="link" title="Remove this property"
            onClick={() => void edit([{ op: 'remove_property', id: eid, property: p.property.iri ?? '' }])}>×</button>
        </>}
      </li>)}
    </ul> : <p className="muted">No static properties.</p>}
    {!viewing && (editing === null
      ? <button className="link" onClick={() => setEditing('pick')}>+ Add property</button>
      : editing === 'pick'
        ? <TermPicker kind="entity_property" allowClear={false} onCancel={() => setEditing(null)}
          onPick={(i) => i && setEditing({ iri: i, label: '', value: '', unit: '' })} />
        : <div className="relation-add">
          <div><span className="rel-name">{editing.label || shortIri(editing.iri)}</span></div>
          {spec?.values?.length ? <select autoFocus value={editing.value} onChange={(e) => setEditing({ ...editing, value: e.target.value })}>
            <option value="" disabled>choose…</option>
            {spec.values.map((v) => <option key={v} value={v}>{v}</option>)}
          </select> : <input autoFocus placeholder={spec?.numeric ? 'number' : 'value'} value={editing.value}
            onChange={(e) => setEditing({ ...editing, value: e.target.value })}
            onKeyDown={(e) => { if (e.key === 'Enter') save(); else if (e.key === 'Escape') setEditing(null) }} />}
          {spec && spec.unit_terms.length > 0 && <select value={editing.unit || (spec.unit_terms.length === 1 ? spec.unit_terms[0].iri : '')}
            onChange={(e) => setEditing({ ...editing, unit: e.target.value })}>
            {spec.unit_terms.length > 1 && <option value="" disabled>unit…</option>}
            {spec.unit_terms.map((u) => <option key={u.iri} value={u.iri}>{u.symbol || u.label}</option>)}
          </select>}
          <button className="link" disabled={!ready} onClick={save}>save</button>
          <button className="link" onClick={() => setEditing(null)}>cancel</button>
        </div>)}
  </>
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
