import { beforeEach, describe, expect, it, vi } from 'vitest'
import { emptySelection, type AgentRun, type Proposal, type ReviewIssue, type Row } from './types'
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

describe('adding issues to the draft', () => {
  const issue = (id: string): ReviewIssue => ({ id, affected_ids: [id], category: 'validation',
    severity: 'violation', origin: 'validation', resolution_state: 'open', explanation: `${id}: missing outlet`,
    details: { findings: [{ focus: `urn:plant:${id}`, message: 'missing outlet', path: 's223:hasConnectionPoint',
      shape: 'urn:shape:outlet', severity: 'Violation' }] } })
  beforeEach(() => {
    useStore.setState({ projectId: 'test', assistantDraft: 'Please connect these to the same pipe.',
      assistantIssueContext: null, rows: new Map(['a', 'b'].map((id) => [id, { id, label: id, kind: 'equipment' }])) as Map<string, Row>,
      selection: emptySelection(), busy: false, runs: {}, proposal: null })
  })

  it('combines separate additions, deduplicates IDs and selects their union', () => {
    useStore.getState().addIssuesToPrompt([issue('a')])
    useStore.getState().addIssuesToPrompt([issue('b'), issue('a')])
    const state = useStore.getState()
    expect(state.assistantDraft.startsWith('Please connect these to the same pipe.')).toBe(true)
    expect(state.assistantIssueContext?.issues.map((i) => i.id)).toEqual(['a', 'b'])
    expect(state.assistantDraft.match(/Please review/g)).toHaveLength(1)
    expect(state.assistantDraft.match(/missing outlet/g)).toHaveLength(1)
    expect(state.selection.entity_ids).toEqual(['a', 'b'])
  })

  it('preserves typed instructions before and after the generated section', () => {
    useStore.getState().addIssuesToPrompt([issue('a')])
    useStore.getState().setAssistantDraft(`${useStore.getState().assistantDraft}\nKeep the inlet unchanged.`)
    useStore.getState().addIssuesToPrompt([issue('b')])
    expect(useStore.getState().assistantDraft.endsWith('Keep the inlet unchanged.')).toBe(true)
    expect(useStore.getState().assistantIssueContext?.issues).toHaveLength(2)
  })

  it('keeps manual edits to the generated block and resets when the draft is cleared', () => {
    useStore.getState().addIssuesToPrompt([issue('a')])
    useStore.getState().setAssistantDraft('My edited issue description')
    useStore.getState().addIssuesToPrompt([issue('b')])
    expect(useStore.getState().assistantDraft.startsWith('My edited issue description')).toBe(true)
    expect(useStore.getState().assistantIssueContext?.issues.map((i) => i.id)).toEqual(['b'])
    useStore.getState().setAssistantDraft('')
    useStore.getState().addIssuesToPrompt([issue('a')])
    expect(useStore.getState().assistantIssueContext?.issues.map((i) => i.id)).toEqual(['a'])
  })
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
