import { useEffect, useState } from 'react'
import { api } from '../api'
import { useStore, type Tab } from '../store'
import {
  emptySelection, type EntityDetail, type IssueDismissalRecord, type Revision, type ReviewIssue, type Row, type ValidationSummary,
} from '../types'
import { shortIri } from './TermPicker'

export function Drawer() {
  const model = useStore((s) => s.model)!
  const inspectId = useStore((s) => s.inspectId)
  const tab = useStore((s) => s.drawerTab)
  const setDrawerTab = useStore((s) => s.setDrawerTab)
  const open = model.issues.filter((i) => i.resolution_state === 'open')
  const problems = open.filter((i) => i.severity !== 'suggestion').length

  // Inspecting an object shows its details, unless its RDF is already open.
  useEffect(() => {
    if (inspectId && useStore.getState().drawerTab !== 'rdf') setDrawerTab('inspector')
  }, [inspectId, setDrawerTab])

  return (
    <section className="drawer">
      <nav className="tabs small">
        <button className={tab === 'issues' ? 'active' : ''}
          title="Validation findings plus source extraction and association issues"
          onClick={() => setDrawerTab('issues')}>
          Issues {problems > 0 && <span className="badge warn">{problems}</span>}
        </button>
        <button className={tab === 'inspector' ? 'active' : ''} onClick={() => setDrawerTab('inspector')}>Inspector</button>
        <button className={tab === 'rdf' ? 'active' : ''} title="The inspected object's RDF, as Turtle"
          onClick={() => setDrawerTab('rdf')}>RDF</button>
        <button className={tab === 'history' ? 'active' : ''} onClick={() => setDrawerTab('history')}>History</button>
      </nav>
      <div className="drawer-body">
        {tab === 'issues' && <IssuesList issues={model.issues} validation={model.revision.validation} />}
        {tab === 'inspector' && <Inspector />}
        {tab === 'rdf' && <RdfView />}
        {tab === 'history' && <History />}
      </div>
    </section>
  )
}

function IssuesList({ issues, validation }: { issues: ReviewIssue[]; validation: ValidationSummary | null }) {
  const rows = useStore((s) => s.rows)
  const setSelection = useStore((s) => s.setSelection)
  const setTab = useStore((s) => s.setTab)
  const inspect = useStore((s) => s.inspect)
  const appendAssistantContext = useStore((s) => s.appendAssistantContext)
  const notify = useStore((s) => s.notify)
  const projectId = useStore((s) => s.projectId)!
  const reload = useStore((s) => s.reload)
  const [showAll, setShowAll] = useState(false)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set())
  const visible = issues.filter((i) => showAll || (i.resolution_state === 'open' && i.severity !== 'suggestion'))
  const hidden = issues.length - visible.length
  const selectedIssues = issues.filter((i) => selectedIds.has(i.id))

  useEffect(() => { setSelectedIds(new Set()) }, [projectId])

  const addSelectedToPrompt = () => {
    if (!selectedIssues.length) return
    const ids = [...new Set(selectedIssues.flatMap((issue) => issue.affected_ids))]
    const rel = ids.filter((id) => rows.get(id)?.kind === 'connection')
    setSelection({ ...emptySelection(), entity_ids: ids.filter((id) => rows.has(id) && !rel.includes(id)),
      relationship_ids: rel })
    appendAssistantContext(issuesPrompt(selectedIssues, rows))
    notify({ kind: 'info', text: `${selectedIssues.length} issue(s) added to the assistant prompt. Review before sending.` })
  }

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
      {validation && <p className="issue-summary muted small">
        Validation: {validation.violations} violations, {validation.warnings} warnings, {validation.suggestions} suggestions.
        The Issues list also includes source extraction and association findings.
      </p>}
      <div className="issue-selection-bar">
        <span>{selectedIssues.length} selected</span>
        <button className="primary" disabled={!selectedIssues.length} onClick={addSelectedToPrompt}>
          Add selected to prompt
        </button>
        {selectedIssues.length > 0 && <button className="link" onClick={() => setSelectedIds(new Set())}>Clear</button>}
      </div>
      <ul className="issues">
        {visible.map((i) => (
          <li key={i.id} className={`issue sev-${i.severity} ${i.resolution_state}`}>
            <input type="checkbox" aria-label={`Select issue: ${i.explanation}`} checked={selectedIds.has(i.id)}
              onChange={() => setSelectedIds((current) => {
                const next = new Set(current)
                if (next.has(i.id)) next.delete(i.id)
                else next.add(i.id)
                return next
              })} />
            <span className={`sev ${i.severity}`}>{i.severity}</span>
            <span className="issue-text" onClick={() => focus(i)} title="Select the affected objects">
              {i.explanation}
              {i.dismissal && <DismissalNote dismissal={i.dismissal} />}
            </span>
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

function useEntityDetail() {
  const projectId = useStore((s) => s.projectId)!
  const head = useStore((s) => s.model?.head)
  const inspectId = useStore((s) => s.inspectId)
  const [detail, setDetail] = useState<EntityDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [version, setVersion] = useState(0)

  useEffect(() => {
    if (!inspectId) { setDetail(null); return }
    setError(null)
    api.entity(projectId, inspectId).then(setDetail).catch((e) => { setDetail(null); setError(String(e.message ?? e)) })
  }, [projectId, inspectId, head, version])

  return { inspectId, detail, error, refresh: () => setVersion((v) => v + 1) }
}

function Inspector() {
  const projectId = useStore((s) => s.projectId)!
  const setSelection = useStore((s) => s.setSelection)
  const appendAssistantContext = useStore((s) => s.appendAssistantContext)
  const notify = useStore((s) => s.notify)
  const reload = useStore((s) => s.reload)
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

function DismissalNote({ dismissal }: { dismissal: IssueDismissalRecord }) {
  return <span className="dismissal-note">
    Dismissed by {dismissal.dismissed_by === 'assistant' ? 'the assistant (applied by you)' : 'you'}
    {dismissal.revision ? ` with ${dismissal.revision}; undoing that change reopens it` : ''}
    {dismissal.reason ? ` — ${dismissal.reason}` : ''}
  </span>
}

function issuesPrompt(issues: ReviewIssue[], rows: Map<string, Row>): string {
  const descriptions = issues.map((issue, index) => {
    const objects = issue.affected_ids.map((id) => {
      const row = rows.get(id)
      return row ? `${row.label} (${row.kind}, ${id})` : id
    })
    const findings = issue.details.findings?.map((finding) => [
      finding.message,
      finding.focus ? `focus ${finding.focus}` : '',
      finding.path ? `path ${finding.path}` : '',
      finding.shape ? `shape ${finding.shape}` : '',
    ].filter(Boolean).join('; ')) ?? []
    return [
      `${index + 1}. [${issue.id}] ${issue.severity}, ${issue.category.replaceAll('_', ' ')}: ${issue.explanation}`,
      objects.length ? `Affected objects: ${objects.join('; ')}` : '',
      findings.length ? `Validation details: ${findings.join(' | ')}` : '',
    ].filter(Boolean).join('\n')
  })
  return [
    'Please review these selected issues together. Check each against the current model, fix them together when they share an underlying cause, and explain any that should be handled separately or no longer apply.',
    `Selected issues (${issues.length}):`,
    descriptions.join('\n\n'),
  ].join('\n\n')
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
