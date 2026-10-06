import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentRun, Proposal } from './types'
import { continuation, threadRuns } from './assistant'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: vi.fn() })
const { useStore } = await import('./store')

const completed = (dismissedId: string): AgentRun => ({
  id: 'refresh', kind: 'correction', input_revision: 'latest', selection: null,
  instruction: 'Reconsider', provider: 'scripted', model: 'scripted', skill_version: '',
  status: 'succeeded', progress: [], outcome: {
    proposal_id: null, dismissed_proposal_id: dismissedId, explanation: 'Already resolved.',
  }, error: null, created_at: '', finished_at: '',
})

describe('clear chat', () => {
  beforeEach(() => {
    useStore.setState({ projectId: 'project', runs: { refresh: completed('old') }, activeRunId: 'refresh',
      proposal: { id: 'pending', status: 'pending' } as Proposal, assistantDraft: 'draft', assistantNewRequest: false,
      assistantCleared: { runs: [], proposals: [] }, autofix: { runId: 'auto' } })
  })

  it('starts a fresh conversation and preserves run usage and the auto-fix tray', () => {
    const run = { ...completed('old'), outcome: { input_tokens: 100, output_tokens: 20 } }
    useStore.setState({ runs: { refresh: run, auto: { ...run, id: 'auto', kind: 'autofix', status: 'running' } } })
    useStore.getState().clearChat()
    const state = useStore.getState()
    expect(state.proposal).toBeNull()
    expect(state.assistantDraft).toBe('')
    expect(state.activeRunId).toBeNull()
    expect(state.autofix).toEqual({ runId: 'auto' })
    expect(state.runs.refresh.outcome.input_tokens).toBe(100)
    expect(continuation(threadRuns(state.runs, state.assistantCleared.runs), state.proposal, state.assistantNewRequest)).toBeNull()
    expect(localStorage.setItem).toHaveBeenCalledWith('workbench.clearedChat.project', JSON.stringify({ runs: ['refresh'], proposals: ['pending'] }))
    // A later server update must not put a cleared message back in the chat.
    useStore.getState().handleEvent({ type: 'run', run })
    expect(threadRuns(useStore.getState().runs, state.assistantCleared.runs)).toEqual([])
  })

  it('keeps an active conversation until the assistant finishes', () => {
    useStore.setState({ runs: { refresh: { ...completed('old'), status: 'running' } } })
    useStore.getState().clearChat()
    expect(useStore.getState().assistantCleared.runs).toEqual([])
    expect(useStore.getState().proposal?.id).toBe('pending')
    expect(useStore.getState().assistantDraft).toBe('draft')
  })
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
