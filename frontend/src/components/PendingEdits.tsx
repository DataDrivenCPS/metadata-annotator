import { useEffect, useMemo, useRef } from 'react'
import { Disclosure } from './Disclosure'
import { autofixGroupOf, pendingItems, proposalOrigin, summarizeChanges, type PendingItem } from '../assistant'
import { headline } from '../autofixView'
import { useStore } from '../store'
import type { AgentRun } from '../types'
import { ProposalPreview } from './AssistantPanel'

/** Every proposed change still waiting for the person, from the chat, auto-fix and builds, each with
 * what it changes and Apply / Discard on the card itself. A choice is one card with its options. */
export function PendingEdits() {
  const edits = useStore((s) => s.pendingEdits)
  const runs = useStore((s) => s.runs)
  const items = useMemo(() => pendingItems(edits, runs), [edits, runs])
  const head = useStore((s) => s.model?.head)
  const stale = items.filter((i) => (i.kind === 'edit' ? [i.proposal] : i.options).some((p) => p.base_revision !== head)).length

  if (!items.length) return <div className="pending-empty muted">
    <p>Nothing is waiting for you.</p>
    <p>Changes the assistant, auto-fix or a build propose appear here until you apply or discard them.</p>
  </div>
  return <div className="pending-edits">
    <p className="pending-intro muted">
      {items.length} waiting{stale > 0 && ` · ${stale} made against an earlier model (applying replays them on the current one)`}
    </p>
    {items.map((item) => <PendingCard key={item.kind === 'edit' ? item.proposal.id : item.options[0].id} item={item} runs={runs} />)}
  </div>
}

function PendingCard({ item, runs }: { item: PendingItem; runs: Record<string, AgentRun> }) {
  const proposals = item.kind === 'edit' ? [item.proposal] : item.options
  const first = proposals[0]
  // A link from the chat or the auto-fix report brings this card into view and marks it for a moment.
  const focused = useStore((s) => !!s.pendingFocus && proposals.some((p) => p.id === s.pendingFocus))
  const ref = useRef<HTMLElement>(null)
  useEffect(() => {
    if (!focused) return
    ref.current?.scrollIntoView({ block: 'start', behavior: 'smooth' })
    const t = window.setTimeout(() => useStore.setState({ pendingFocus: null }), 2500)
    return () => window.clearTimeout(t)
  }, [focused])
  const open = useStore((s) => !s.pendingCollapsed.includes(first.id))
  const toggle = useStore((s) => s.togglePending)
  const when = new Date(first.created_at).toLocaleString()
  // An auto-fix edit is named by the issue it fixes; its request is the run's own wording.
  const group = autofixGroupOf(first, runs)
  const about = group ? (group.status === 'choice' ? group.explanations[0] : headline(group)) : first.instruction.trim()
  const title = item.kind === 'edit' ? item.proposal.explanation || about : item.offered.choice.question
  const counts = useMemo(() => {
    if (item.kind === 'choice') return `${item.options.length} options`
    const c = { created: 0, updated: 0, deleted: 0 }
    for (const l of summarizeChanges(item.proposal.changes, (f) => f)) c[l.change]++
    return [c.created && `${c.created} added`, c.updated && `${c.updated} changed`, c.deleted && `${c.deleted} removed`]
      .filter(Boolean).join(' · ')
  }, [item])
  return <article ref={ref} className={`pending-card${focused ? ' focused' : ''}${open ? ' open' : ''}`}>
    <button className="bare pending-card-head" aria-expanded={open} onClick={() => toggle(first.id)}>
      <span className="pending-chevron" aria-hidden="true">
        <svg viewBox="0 0 16 16" width="14" height="14"><path d="M6 3.5 10.5 8 6 12.5" fill="none" stroke="currentColor"
          strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg></span>
      <span className="pending-origin">{proposalOrigin(first, runs)}</span>
      <span className="pending-title">{title}</span>
      {counts && <span className="pending-counts muted small">{counts}</span>}
      <span className="pending-when muted small" title={when}>{new Date(first.created_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</span>
    </button>
    {open && <div className="pending-card-body">
    {about && about !== title && <div className="pending-request muted small" title={about}>{group ? `For: ${about}` : `“${about}”`}</div>}
    {!!group?.reasons?.length && <Disclosure className="pending-reasons small" summary="Why auto-fix did not apply it">
      <ul>{group.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
    </Disclosure>}
    {item.kind === 'edit'
      ? <>
        <ProposalPreview proposal={item.proposal} running={false} />
      </>
      : <>
        {item.options.map((p) => {
          const option = item.offered.choice.options.find((o) => o.proposal_id === p.id)
          return <ProposalPreview key={p.id} proposal={p} running={false} applyLabel="Choose this"
            title={<>{option?.label ?? 'Option'}{option?.note && <span className="muted small"> · {option.note}</span>}</>} />
        })}
      </>}
    </div>}
  </article>
}
