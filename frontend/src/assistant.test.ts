import { describe, expect, it } from 'vitest'
import { continuation, proposalStates, startsExchange, threadRuns } from './assistant'
import type { AgentRun, Proposal } from './types'

const run = (id: string, created_at: string, extra: Partial<AgentRun> = {}): AgentRun => ({
  id, kind: 'correction', input_revision: 'rev-1', selection: null, instruction: id, provider: 'p', model: 'm',
  skill_version: '', status: 'succeeded', progress: [], outcome: {}, error: null, created_at, finished_at: created_at,
  ...extra,
})

describe('assistant thread', () => {
  it('orders runs by request time', () => {
    const t = threadRuns({ b: run('b', '2026-01-02'), a: run('a', '2026-01-01') })
    expect(t.map((r) => r.id)).toEqual(['a', 'b'])
  })

  it('answers questions from the last run', () => {
    const asked = run('a', '1', { outcome: { questions: ['Which tank?'], proposal_id: null } })
    expect(continuation([asked], null, false)).toEqual({ kind: 'questions', runId: 'a' })
  })

  it('replies to a pending proposal, continuing from the latest finished run', () => {
    const pending = { id: 'p1', status: 'pending' } as Proposal
    const t = [run('a', '1', { outcome: { proposal_id: 'p1' } })]
    expect(continuation(t, pending, false)).toEqual({ kind: 'proposal', runId: 'a' })
    expect(continuation([...t, run('b', '2', { status: 'running' })], pending, false))
      .toEqual({ kind: 'proposal', runId: null })
  })

  it('starts fresh when asked to or when there is nothing to continue', () => {
    expect(continuation([run('a', '1')], null, true)).toBeNull()
    expect(continuation([], null, false)).toBeNull()
    expect(continuation([run('a', '1')], null, false)).toEqual({ kind: 'conversation', runId: 'a' })
  })

  it('marks new exchanges and revised proposals', () => {
    expect(startsExchange(run('a', '1'))).toBe(true)
    expect(startsExchange(run('b', '2', { parent_run_id: 'a' }))).toBe(false)
    expect(startsExchange(run('c', '3', { mode: 'reply' }))).toBe(false)
    expect(proposalStates([
      { id: 'p1', status: 'dismissed' } as Proposal,
      { id: 'p2', status: 'pending', parent_proposal_id: 'p1' } as Proposal,
    ])).toEqual({ p1: 'superseded', p2: 'pending' })
  })
})
