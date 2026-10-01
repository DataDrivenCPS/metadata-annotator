import type { AgentRun, Proposal, Selection } from './types'

export type ProposalState = Proposal['status'] | 'superseded'

/** What the next message continues: the pending proposal, the assistant's questions, or the conversation. */
export type Continuation = { kind: 'proposal' | 'questions' | 'conversation'; runId: string | null }

export const isActive = (run: AgentRun) => run.status === 'queued' || run.status === 'running'

/** Assistant runs in the order they were requested. */
export function threadRuns(runs: Record<string, AgentRun>): AgentRun[] {
  return Object.values(runs).sort((a, b) => a.created_at.localeCompare(b.created_at))
}

/** A run that does not continue an earlier one starts a new exchange in the thread. */
export const startsExchange = (run: AgentRun) =>
  run.mode === 'build' || ((run.mode ?? 'assist') === 'assist' && !run.parent_run_id)

export function continuation(thread: AgentRun[], proposal: Proposal | null, newRequest: boolean): Continuation | null {
  if (newRequest) return null
  const last = thread[thread.length - 1]
  const runId = last && !isActive(last) ? last.id : null
  if (proposal?.status === 'pending') return { kind: 'proposal', runId }
  if (!last || !runId) return null
  if (last.status === 'succeeded' && !last.outcome.proposal_id && last.outcome.questions?.length) {
    return { kind: 'questions', runId }
  }
  return { kind: 'conversation', runId }
}

/** Proposal states for the thread: a proposal revised by a later reply is superseded. */
export function proposalStates(proposals: Proposal[]): Record<string, ProposalState> {
  const out: Record<string, ProposalState> = {}
  for (const p of proposals) out[p.id] = p.status
  for (const p of proposals) if (p.parent_proposal_id && out[p.parent_proposal_id]) out[p.parent_proposal_id] = 'superseded'
  return out
}

export type SelectionSource = 'current' | 'conversation' | 'proposal'

const isEmpty = (s: Selection) =>
  !s.entity_ids.length && !s.relationship_ids.length && !s.field_ids.length && !s.source_regions.length

/** The selection the next message will be about, matching what the server uses. */
export function effectiveSelection(next: Continuation | null, current: Selection, thread: AgentRun[],
                                   proposal: Proposal | null): { selection: Selection; source: SelectionSource } {
  if (next?.kind === 'proposal' && proposal) return { selection: proposal.selection, source: 'proposal' }
  const parent = next?.runId ? thread.find((r) => r.id === next.runId) : null
  if (parent?.selection && isEmpty(current) && !isEmpty(parent.selection)) {
    return { selection: parent.selection, source: 'conversation' }
  }
  return { selection: current, source: 'current' }
}
