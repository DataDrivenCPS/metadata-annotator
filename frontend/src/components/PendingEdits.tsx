import { useEffect, useMemo, useRef } from 'react'
import { Disclosure } from './Disclosure'
import { autofixGroupOf, pendingItems, proposalOrigin, type PendingItem } from '../assistant'
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
  const when = new Date(first.created_at).toLocaleString()
  // An auto-fix edit is named by the issue it fixes; its request is the run's own wording.
  const group = autofixGroupOf(first, runs)
  const about = group ? (group.status === 'choice' ? group.explanations[0] : headline(group)) : first.instruction.trim()
  return <article ref={ref} className={`pending-card${focused ? ' focused' : ''}`}>
    <header className="pending-card-head">
      <span className="pending-origin">{proposalOrigin(first, runs)}</span>
      <span className="muted small" title={when}>{when}</span>
      {about && <span className="pending-request muted small" title={about}>{group ? `For: ${about}` : `“${about}”`}</span>}
    </header>
    {!!group?.reasons?.length && <Disclosure className="pending-reasons small" summary="Why auto-fix did not apply it">
      <ul>{group.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
    </Disclosure>}
    {item.kind === 'edit'
      ? <>
        {item.proposal.explanation && <p className="pending-explanation">{item.proposal.explanation}</p>}
        <ProposalPreview proposal={item.proposal} running={false} />
      </>
      : <>
        <p className="pending-question"><strong>{item.offered.choice.question}</strong>
          <span className="muted small"> · choosing one discards the other{item.options.length > 2 ? 's' : ''}</span></p>
        {item.options.map((p) => {
          const option = item.offered.choice.options.find((o) => o.proposal_id === p.id)
          return <ProposalPreview key={p.id} proposal={p} running={false} applyLabel="Choose this"
            title={<>{option?.label ?? 'Option'}{option?.note && <span className="muted small"> · {option.note}</span>}</>} />
        })}
      </>}
  </article>
}
