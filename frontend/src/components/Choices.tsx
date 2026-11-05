import { useState, type ReactNode } from 'react'
import type { AutofixChoice } from '../assistant'
import { choiceOutcome } from '../autofixView'
import { useStore } from '../store'

/** A button that shows a spinner while its action runs, so a slow click visibly registers. */
export function ActionButton({ onClick, disabled, children, ...rest }: {
  onClick: () => Promise<unknown>; disabled?: boolean; children: ReactNode; className?: string; title?: string
}) {
  const [pending, setPending] = useState(false)
  return <button {...rest} disabled={disabled || pending} aria-busy={pending} onClick={() => {
    setPending(true)
    void onClick().finally(() => setPending(false))
  }}>{pending && <span className="spinner small" />}{children}</button>
}

/** One question with its options; once one is applied, just the answer. Each option opens its card in
 * the Pending edits tab, where it is reviewed and chosen. */
export function ChoiceButtons({ choice }: { choice: AutofixChoice }) {
  const showPending = useStore((s) => s.showPending)
  const states = useStore((s) => s.proposalStates)
  const { chosen, closed } = choiceOutcome(choice, states)
  return <div className="autofix-choice">
    <div className="small">{choice.question}</div>
    {chosen ? <div className="small">✓ {chosen.label}</div>
      : closed ? <div className="muted small">No longer offered.</div>
      : <div className="autofix-options">{choice.options.map((o) =>
          <button key={o.proposal_id} disabled={states[o.proposal_id] === 'dismissed'}
            title={`See this option in Pending edits, where you choose it${o.note ? `; it ${o.note}` : ''}`}
            onClick={() => showPending(o.proposal_id)}>
            {o.label}{o.note && <span className="muted"> · {o.note}</span>} →
          </button>)}</div>}
  </div>
}
