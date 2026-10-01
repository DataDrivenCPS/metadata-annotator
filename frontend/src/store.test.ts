import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentRun, Proposal } from './types'

vi.stubGlobal('localStorage', { getItem: () => null })
const { useStore } = await import('./store')

const completed = (dismissedId: string): AgentRun => ({
  id: 'refresh', kind: 'correction', input_revision: 'latest', selection: null,
  instruction: 'Reconsider', provider: 'scripted', model: 'scripted', skill_version: '',
  status: 'succeeded', progress: [], outcome: {
    proposal_id: null, dismissed_proposal_id: dismissedId, explanation: 'Already resolved.',
  }, error: null, created_at: '', finished_at: '',
})

describe('refresh completion', () => {
  beforeEach(() => {
    useStore.setState({
      proposal: { id: 'old' } as Proposal, activeRunId: 'refresh', runs: {},
      proposalStates: {}, assistantDraft: 'My next request',
    })
  })

  it('removes the resolved proposal while preserving the draft and explanation', () => {
    useStore.getState().handleEvent({ type: 'run', run: completed('old') })
    const state = useStore.getState()
    expect(state.proposal).toBeNull()
    expect(state.proposalStates.old).toBe('dismissed')
    expect(state.assistantDraft).toBe('My next request')
    expect(state.runs.refresh.outcome.explanation).toBe('Already resolved.')
  })

  it('preserves a different proposal when an earlier refresh finishes', () => {
    useStore.setState({ proposal: { id: 'new' } as Proposal })
    useStore.getState().handleEvent({ type: 'run', run: completed('old') })
    expect(useStore.getState().proposal?.id).toBe('new')
  })
})
