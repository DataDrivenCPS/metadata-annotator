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

/** One question with its options as buttons; once one is applied, just the answer. Auto-fix options
 * were checked against the issues, so picking one applies it; a chat option opens for review first. */
export function ChoiceButtons({ choice, review = false }: { choice: AutofixChoice; review?: boolean }) {
  const choose = useStore((s) => s.chooseOption)
  const reviewProposal = useStore((s) => s.reviewProposal)
  const states = useStore((s) => s.proposalStates)
  const openId = useStore((s) => s.proposal?.id)
  const busy = useStore((s) => s.busy)
  const viewing = useStore((s) => s.viewing)
  const { chosen, closed } = choiceOutcome(choice, states)
  return <div className="autofix-choice">
    <div className="small">{choice.question}</div>
    {chosen ? <div className="small">✓ {chosen.label}</div>
      : closed ? <div className="muted small">No longer offered.</div>
      : <div className="autofix-options">{choice.options.map((o) => {
          const open = review && openId === o.proposal_id
          return <ActionButton key={o.proposal_id} className={open ? 'chosen' : undefined}
            disabled={busy || (!review && !!viewing) || states[o.proposal_id] === 'dismissed'}
            title={review ? `Shows this option as a proposed change to review and apply${o.note ? `; it ${o.note}` : ''}`
              : o.note ? `Applies this choice; ${o.note}` : 'Applies this choice'}
            onClick={() => review ? reviewProposal(o.proposal_id) : choose(choice, o.proposal_id)}>
            {open && '▸ '}{o.label}{o.note && <span className="muted"> · {o.note}</span>}
          </ActionButton>
        })}</div>}
  </div>
}
