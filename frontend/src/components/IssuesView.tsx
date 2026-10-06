import { useEffect, useState } from 'react'
import { api } from '../api'
import { autofixCandidates, isActive } from '../assistant'
import { useStore } from '../store'
import { emptySelection, type IssueDismissalRecord, type IssueRepair, type ReviewIssue } from '../types'

export function IssuesView() {
  const issues = useStore((s) => s.model!.issues)
  const validation = useStore((s) => s.model!.revision.validation)
  const rows = useStore((s) => s.rows)
  const setSelection = useStore((s) => s.setSelection)
  const setTab = useStore((s) => s.setTab)
  const inspect = useStore((s) => s.inspect)
  const addIssuesToPrompt = useStore((s) => s.addIssuesToPrompt)
  const notify = useStore((s) => s.notify)
  const projectId = useStore((s) => s.projectId)!
  const reload = useStore((s) => s.reload)
  const repairs = useStore((s) => s.repairs?.byIssue)
  const startAutofix = useStore((s) => s.startAutofix)
  const autofixing = useStore((s) => !!s.autofix)
  const viewing = useStore((s) => s.viewing)
  const running = useStore((s) => Object.values(s.runs).some(isActive))
  const [showAll, setShowAll] = useState(false)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set())
  const visible = issues.filter((i) => showAll || (i.resolution_state === 'open' && i.severity !== 'suggestion'))
  const hidden = issues.length - visible.length
  const selectedIssues = issues.filter((i) => selectedIds.has(i.id))

  useEffect(() => { setSelectedIds(new Set()) }, [projectId])

  const addToPrompt = (chosen: ReviewIssue[]) => {
    if (!chosen.length) return
    addIssuesToPrompt(chosen)
    notify({ kind: 'info', text: `${chosen.length} issue(s) added to the assistant prompt. Review before sending.` })
  }

  // Select and inspect the affected objects, staying on this list to work through it.
  const focus = (i: ReviewIssue) => {
    const ids = i.affected_ids.filter((id) => rows.has(id))
    const rel = ids.filter((id) => rows.get(id)!.kind === 'connection')
    setSelection({ ...emptySelection(), entity_ids: ids.filter((id) => !rel.includes(id)), relationship_ids: rel })
    if (ids[0]) inspect(ids[0])
  }
  const showInTable = (i: ReviewIssue) => {
    focus(i)
    const kind = rows.get(i.affected_ids.find((id) => rows.has(id)) ?? '')?.kind
    if (kind) setTab(kind === 'point' ? 'points' : kind === 'connection' ? 'connections' : 'equipment')
  }

  if (!issues.length) return <p className="muted pad">No issues: the model passes validation against the loaded vocabulary.</p>
  return (
    <div className="issues-view">
      {validation && (() => {
        const fromValidation = issues.filter((i) => i.origin === 'validation')
        const count = (severity: string) => fromValidation.filter((i) => i.severity === severity).length
        return <p className="issue-summary muted small">
          Validation: {count('violation')} violations, {count('warning')} warnings, {count('suggestion')} suggestions.
          The Issues list also includes source extraction and association findings.
        </p>
      })()}
      <div className="issue-selection-bar">
        <span>{selectedIssues.length} selected</span>
        <button className="primary" disabled={!selectedIssues.length} onClick={() => addToPrompt(selectedIssues)}>
          Add selected to prompt
        </button>
        {selectedIssues.length > 0 && <button className="link" onClick={() => setSelectedIds(new Set())}>Clear</button>}
        {(() => {
          const queue = autofixCandidates(issues, selectedIds)
          return !viewing && <button disabled={!queue.length || autofixing || running}
            title={autofixing ? 'Auto-fix is already running' : running ? 'Wait for the assistant to finish'
              : 'The assistant fixes these group by group; fixes that pass every check are applied (and can be undone), the rest wait for your review or input'}
            onClick={() => { setSelectedIds(new Set()); void startAutofix(queue) }}>
            {selectedIssues.length ? `Auto-fix selected (${queue.length})` : `Auto-fix ${queue.length} violation${queue.length === 1 ? '' : 's'}`}
          </button>
        })()}
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
            <span className="issue-text" onClick={() => focus(i)} title="Select and inspect the affected object">
              <span className="issue-message">{i.explanation}</span>
              {repairs?.[i.id] && <RepairLine repair={repairs[i.id]} />}
              {i.dismissal && <DismissalNote dismissal={i.dismissal} />}
            </span>
            <span className="issue-actions">
              {!viewing && <button className="link" onClick={() => addToPrompt([i])}
                title="Add this issue to the assistant prompt and select its objects">add to chat</button>}
              {i.affected_ids.some((id) => rows.has(id)) && <button className="link" onClick={() => showInTable(i)}>show in table</button>}
              {!viewing && <button className="link" onClick={() => void api.setIssueState(projectId, i.id,
                i.resolution_state === 'dismissed' ? 'open' : 'dismissed').then(reload)}>
                {i.resolution_state === 'dismissed' ? 'reopen' : 'dismiss'}
              </button>}
            </span>
          </li>
        ))}
      </ul>
      {hidden > 0 && <button className="link pad" onClick={() => setShowAll(!showAll)}>
        {showAll ? 'hide suggestions and dismissed' : `show ${hidden} suggestion(s)/dismissed`}</button>}
    </div>
  )
}

/** One line of pyshifty's repair witness: its failing leaves, in the engine's words. */
export function RepairLine({ repair }: { repair: IssueRepair }) {
  return <span className="repair-line" title="From the repair engine (pyshifty, via BuildingMOTIF's algebraic validation)">
    {repair.blocked && <span className="repair-tag">blocked</span>}
    {repair.summary.join('; ')}
  </span>
}

export function RepairDetail({ repair }: { repair: IssueRepair }) {
  return <details className="repair-detail" open>
    <summary>Repair engine (pyshifty){repair.shape ? ` · shape ${repair.shape}` : ''}{repair.blocked ? ' · blocked' : ''}</summary>
    <ul>{repair.summary.map((s) => <li key={s}>{s}</li>)}</ul>
    {repair.missing.length > 0 && <><h6>Missing</h6><ul>{repair.missing.map((m) => <li key={m}>{m}</li>)}</ul></>}
    {repair.offending.length > 0 && <><h6>Offending values</h6><ul>{repair.offending.map((o) => <li key={o}>{o}</li>)}</ul></>}
    {repair.repair && <><h6>{repair.blocked ? 'Repair tree' : 'Edits that would satisfy it'}</h6><pre className="code">{repair.repair}</pre></>}
  </details>
}

export function DismissalNote({ dismissal }: { dismissal: IssueDismissalRecord }) {
  return <span className="dismissal-note">
    Dismissed by {dismissal.dismissed_by === 'assistant' ? 'the assistant (applied by you)' : 'you'}
    {dismissal.revision ? ` with ${dismissal.revision}; undoing that change reopens it` : ''}
    {dismissal.reason ? ` — ${dismissal.reason}` : ''}
  </span>
}
