import { useMemo, useState } from 'react'
import { autofixProgress, autofixReport, isActive, issueSelection, pendingCount, type AutofixGroup, type AutofixStatus } from '../assistant'
import { affectedRows, groupIssues, headline, isHandled, labelList, reportSections, STATUS_INFO, tally,
  type ReportSection } from '../autofixView'
import { useStore } from '../store'
import type { AgentRun, ReviewIssue, Row } from '../types'
import { ChoiceButtons } from './Choices'

const NO_ISSUES: ReviewIssue[] = []

/** A running auto-fix (group k of n), or its report: what was fixed, and what still needs the person. */
export function AutofixBanner() {
  const af = useStore((s) => s.autofix)
  const run = useStore((s) => (s.autofix ? s.runs[s.autofix.runId] : undefined))
  const states = useStore((s) => s.proposalStates)
  const stop = useStore((s) => s.stopAutofix)
  if (!af) return null
  if (!run || isActive(run)) {
    const { groups } = autofixReport(run)
    const { group, total, message } = autofixProgress(run)
    const { fixed, forYou } = tally(groups, states)
    return <div className="autofix-banner working">
      <div className="autofix-head">
        <strong>Auto-fix{total ? ` · group ${Math.max(group, 1)} of ${total}` : ''}</strong>
        {groups.length > 0 && <span className="muted">{fixed} fixed · {forYou} for you</span>}
        <span className="spacer" /><button onClick={() => void stop()}>Stop</button>
      </div>
      <div className="autofix-state"><span className="spinner small" /> {message}</div>
    </div>
  }
  return <AutofixReport key={run.id} run={run} />
}

function summaryText(run: AgentRun, total: number, { fixed, forYou, handled }: ReturnType<typeof tally>): string {
  if (run.status !== 'succeeded') return run.error || run.status
  if (!total) return 'Nothing to fix.'
  const left = forYou - handled
  const parts = []
  if (fixed) parts.push(`${fixed} fixed automatically`)
  if (forYou) parts.push(left ? `${left} need${left === 1 ? 's' : ''} you` : `all ${forYou} you were asked about handled`)
  return parts.join(' · ')
}

function AutofixReport({ run }: { run: AgentRun }) {
  const head = useStore((s) => s.model?.head)
  const states = useStore((s) => s.proposalStates)
  const stop = useStore((s) => s.stopAutofix)
  const undoAll = useStore((s) => s.undoAutofix)
  const { groups, revisions, sections } = useMemo(() => {
    const report = autofixReport(run)
    return { ...report, sections: reportSections(report.groups) }
  }, [run])
  // Which groups the person has dealt with, worked out once for the summary, the sections and the items.
  const handled = useMemo(() => new Set(groups.flatMap((g, key) => isHandled(g, states) ? [key] : [])), [groups, states])
  const counts = tally(groups, states)
  // Open only when something needs the person outside Pending edits (questions, failures): edits
  // and choices wait in that tab, so the report stays a one-line summary with a link to it.
  const [open, setOpen] = useState(groups.some((g) => g.status === 'input' || g.status === 'failed'))
  const [filter, setFilter] = useState<AutofixStatus | null>(null)
  // The first item to act on starts expanded, so its options and buttons are in view.
  const [expanded, setExpanded] = useState<Set<number>>(() => {
    const first = sections.find((s) => STATUS_INFO[s.status].needsYou)?.items.find((i) => !handled.has(i.key))
    return new Set(first ? [first.key] : [])
  })
  const toggle = (key: number) => setExpanded((prev) => {
    const next = new Set(prev)
    if (!next.delete(key)) next.add(key)
    return next
  })
  const shown = filter ? sections.filter((s) => s.status === filter) : sections

  return <details open={open} onToggle={(e) => setOpen(e.currentTarget.open)}
    className={`autofix-banner autofix-tray ${run.status === 'succeeded' ? 'done' : 'failed'}`}>
    <summary className="autofix-tray-toggle">
      <span className="autofix-tray-chevron" aria-hidden="true">▸</span>
      <span className="autofix-tray-summary">
        <strong>Auto-fix {run.status === 'succeeded' ? 'finished' : run.status}</strong>
        <span className="muted">{summaryText(run, groups.length, counts)}</span>
        <PendingLink run={run} />
      </span>
      <span className="autofix-tray-hint">
        <span className="autofix-expand">Show results</span>
        <span className="autofix-collapse">Hide results</span>
      </span>
      <button className="bare autofix-close" aria-label="Close the auto-fix report"
        title="Close this report; proposals it left stay available"
        onClick={(e) => { e.preventDefault(); e.stopPropagation(); void stop() }}>×</button>
    </summary>
    <div className="autofix-tray-body">
      <div className="autofix-head afx-toolbar">
        {sections.length > 1 && <div className="afx-filters" role="group" aria-label="Show only">
          <button className={`afx-filter${filter === null ? ' on' : ''}`} aria-pressed={filter === null}
            onClick={() => setFilter(null)}>All {groups.length}</button>
          {sections.map((s) => <button key={s.status} className={`afx-filter${filter === s.status ? ' on' : ''}`}
            aria-pressed={filter === s.status} title={STATUS_INFO[s.status].hint}
            onClick={() => setFilter(filter === s.status ? null : s.status)}>
            {STATUS_INFO[s.status].badge} {s.items.length}
          </button>)}
        </div>}
        <span className="spacer" />
        {head && revisions.includes(head) &&
          <button onClick={() => void undoAll()} title="Undo every automatic fix from this run">Undo automatic fixes</button>}
        <button onClick={() => void stop()} title="Close this report; proposals it left stay available">Close</button>
      </div>
      {shown.map((section) => <ReportSectionView key={section.status} section={section} handled={handled}
        expanded={expanded} onToggle={toggle} onContinue={() => setOpen(false)} forced={filter === section.status} />)}
    </div>
  </details>
}

/** A section of one kind of result; sections that need the person start open, finished ones closed
 * (filtering to a section keeps it open). */
function ReportSectionView({ section: { status, items }, handled, expanded, onToggle, onContinue, forced }: {
  section: ReportSection; handled: Set<number>; expanded: Set<number>
  onToggle: (key: number) => void; onContinue: () => void; forced: boolean
}) {
  const info = STATUS_INFO[status]
  const [open, setOpen] = useState(info.needsYou)
  const isOpen = open || forced
  const left = items.filter((i) => !handled.has(i.key)).length
  return <section className="afx-section">
    <button className="bare afx-section-head" aria-expanded={isOpen} onClick={() => !forced && setOpen(!open)}>
      <span className="afx-chevron" aria-hidden="true">▸</span>
      <span>{info.heading}</span>
      <span className="afx-count">{left !== items.length ? `${left} of ${items.length} left` : items.length}</span>
    </button>
    {isOpen && <>
      <p className="afx-hint">{info.hint}</p>
      <ul className="afx-items">{items.map(({ key, group }) =>
        <ReportItemView key={key} group={group} handled={handled.has(key)} open={expanded.has(key)}
          onToggle={() => onToggle(key)} onContinue={onContinue} />)}
      </ul>
    </>}
  </section>
}

/** One group: a single summary line. An edit or choice still waiting opens its card in Pending edits;
 * anything else shows everything known about it and what can be done. */
function ReportItemView({ group, handled, open, onToggle, onContinue }: {
  group: AutofixGroup; handled: boolean; open: boolean; onToggle: () => void; onContinue: () => void
}) {
  const issues = useStore((s) => s.model?.issues) ?? NO_ISSUES
  const rows = useStore((s) => s.rows)
  const mine = useMemo(() => groupIssues(group, issues), [group, issues])
  const affected = useMemo(() => affectedRows(mine, rows), [mine, rows])
  const showPending = useStore((s) => s.showPending)
  const edit = !handled ? group.proposal_id ?? group.choices?.[0]?.options[0]?.proposal_id : undefined
  return <li className={`afx-item ${group.status}${handled ? ' handled' : ''}${open ? ' open' : ''}`}>
    <button className="bare afx-item-head" aria-expanded={edit ? undefined : open}
      onClick={edit ? () => { showPending(edit); onContinue() } : onToggle}
      title={edit ? 'Open it in Pending edits, where you review and apply it'
        : open ? 'Hide the details' : 'Show the details and what you can do'}>
      <span className={`afx-badge ${group.status}`}>{handled ? '✓ Done' : STATUS_INFO[group.status].badge}</span>
      <span className="afx-item-text">
        <span className="afx-headline">{headline(group)}</span>
        {(affected.length > 0 || group.issues.length > 1) && <span className="afx-meta">
          {[affected.length > 0 && labelList(affected), group.issues.length > 1 && `${group.issues.length} issues`]
            .filter(Boolean).join(' · ')}
        </span>}
      </span>
      <span className={edit ? 'afx-go' : 'afx-chevron'} aria-hidden="true">{edit ? '→' : '▸'}</span>
    </button>
    {open && !edit && <ItemDetail group={group} mine={mine} affected={affected} onContinue={onContinue} />}
  </li>
}

/** The review action: the change waits in the Pending edits tab, with Apply and Discard on it. */
function ReviewAction({ proposalId, onContinue }: { proposalId: string; onContinue: () => void }) {
  const showPending = useStore((s) => s.showPending)
  const state = useStore((s) => s.proposalStates[proposalId])
  if (state === 'applied') return <span className="small ok-text">✓ Applied</span>
  if (state === 'dismissed') return <span className="muted small">Discarded</span>
  return <button className="primary" onClick={() => { showPending(proposalId); onContinue() }}
    title="The change waits in Pending edits: see what it changes, then apply or discard it">Open in Pending edits</button>
}

/** The edits from this run still waiting, with a way to them. */
function PendingLink({ run }: { run: AgentRun }) {
  const showPending = useStore((s) => s.showPending)
  const edits = useStore((s) => s.pendingEdits)
  const runs = useStore((s) => s.runs)
  const waiting = useMemo(() => pendingCount(edits, runs, run.id), [edits, runs, run.id])
  if (!waiting) return null
  return <button className="link" onClick={(e) => { e.preventDefault(); e.stopPropagation(); showPending() }}>
    {waiting} pending edit{waiting === 1 ? '' : 's'} →</button>
}

function ItemDetail({ group, mine, affected, onContinue }: {
  group: AutofixGroup; mine: ReviewIssue[]; affected: Row[]; onContinue: () => void
}) {
  const addIssuesToPrompt = useStore((s) => s.addIssuesToPrompt)
  const setSelection = useStore((s) => s.setSelection)
  const inspect = useStore((s) => s.inspect)
  const viewing = useStore((s) => s.viewing)
  const info = STATUS_INFO[group.status]
  const open = mine.filter((i) => i.resolution_state === 'open')
  // Select what the issues affect and inspect one (the drawer switches to the inspector by itself).
  const show = (row = affected[0]) => {
    setSelection(issueSelection(mine, useStore.getState().rows))
    inspect(row?.id ?? null)
  }

  return <div className="afx-detail">
    {group.status === 'choice' && group.choices?.map((c, k) => <ChoiceButtons key={k} choice={c} />)}
    {group.status === 'review' && group.proposal_id &&
      <div className="afx-primary"><ReviewAction proposalId={group.proposal_id} onContinue={onContinue} /></div>}
    {group.status === 'input' && !!group.questions?.length && <div>
      <h5 className="afx-label">The assistant asks</h5>
      <ul>{group.questions.map((q) => <li key={q}>{q}</li>)}</ul>
    </div>}

    <div>
      <h5 className="afx-label">{group.explanations.length > 1 ? `Issues (${group.explanations.length})` : 'Issue'}</h5>
      <ul>{group.explanations.map((e, k) => <li key={k}>{e}</li>)}</ul>
    </div>
    {affected.length > 0 && <div>
      <h5 className="afx-label">Affects</h5>
      <div className="chips afx-entities">{affected.map((r) =>
        <button key={r.id} className={`chip ${r.kind}`} onClick={() => show(r)} title={`Select ${r.label || r.id} and open it in the inspector`}>
          {r.label || r.id}<span className="muted"> · {r.kind.replace(/_/g, ' ')}</span>
        </button>)}</div>
    </div>}
    {group.explanation && <div>
      <h5 className="afx-label">{info.explanationLabel}</h5>
      <div>{group.explanation}</div>
    </div>}
    {info.reasonsLabel && !!group.reasons?.length && <div>
      <h5 className="afx-label">{info.reasonsLabel}</h5>
      <ul>{group.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
    </div>}

    <div className="afx-actions">
      {affected.length > 0 && <button onClick={() => show()}>Show in model</button>}
      {info.needsYou && !viewing && open.length > 0 && <button onClick={() => { addIssuesToPrompt(open); onContinue() }}
        title="Put these issues in the assistant prompt so you can explain what you want">{info.discussLabel}</button>}
    </div>
  </div>
}
