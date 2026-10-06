import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentRun, ModelResponse, ReviewIssue } from './types'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {} })

// The "server": the head moves as revisions are undone.
let head = 'rev-3'
const issue = (id: string): ReviewIssue => ({
  id, affected_ids: [`eq-${id}`], category: 'validation', severity: 'violation', explanation: `problem ${id}`,
  resolution_state: 'open', origin: 'validation', details: {},
})
const model = () => ({
  head, info: { family: 's223' },
  view: { equipment: [], points: [], connections: [], connection_points: [], containment: [] },
  issues: ['a', 'b'].map(issue), revision: { validation: null },
}) as unknown as ModelResponse
const run = (status: AgentRun['status'], outcome: AgentRun['outcome'] = {}): AgentRun => ({
  id: 'run-af', kind: 'autofix', input_revision: 'rev-1', selection: null, instruction: '', provider: 'p', model: 'm',
  skill_version: '', status, progress: [], outcome, error: null, created_at: '', finished_at: '',
})

vi.mock('./api', () => ({
  ApiError: class extends Error {},
  api: {
    model: vi.fn(async () => model()),
    repairs: vi.fn(async () => ({})),
    autofix: vi.fn(async () => run('queued')),
    cancelRun: vi.fn(async () => run('cancelled')),
    undo: vi.fn(async () => { head = `rev-${Number(head.slice(4)) - 1}`; return { head } }),
    proposal: vi.fn(async (_pid: string, id: string) => ({ id, status: 'pending' })),
  },
}))

const { api } = await import('./api')
const { useStore } = await import('./store')

describe('auto-fix', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    head = 'rev-3'
    useStore.setState({ projectId: 'p', model: model(), rows: new Map(), runs: {}, proposal: null, autofix: null, viewing: null })
  })

  it('starts one background run for the chosen issues', async () => {
    await useStore.getState().startAutofix(model().issues)
    expect(vi.mocked(api.autofix).mock.calls[0].slice(1, 3)).toEqual(['rev-3', ['a', 'b']])
    expect(useStore.getState().autofix).toEqual({ runId: 'run-af' })
  })

  it('stop cancels a running auto-fix, then closes the report', async () => {
    await useStore.getState().startAutofix(model().issues)
    await useStore.getState().stopAutofix()
    expect(api.cancelRun).toHaveBeenCalledTimes(1)
    expect(useStore.getState().autofix).not.toBeNull()  // the cancelled run's report stays until closed
    await useStore.getState().stopAutofix()
    expect(useStore.getState().autofix).toBeNull()
  })

  it('undoes the automatic revisions while they are newest', async () => {
    const done = run('succeeded', { autofix: { groups: [], revisions: ['rev-2', 'rev-3'] } })
    useStore.setState({ autofix: { runId: 'run-af' }, runs: { 'run-af': done } })
    await useStore.getState().undoAutofix()
    expect(api.undo).toHaveBeenCalledTimes(2)
    expect(useStore.getState().model!.head).toBe('rev-1')
  })

  it('leaves later changes alone', async () => {
    const done = run('succeeded', { autofix: { groups: [], revisions: ['rev-2'] } })  // rev-3 is someone's edit
    useStore.setState({ autofix: { runId: 'run-af' }, runs: { 'run-af': done } })
    await useStore.getState().undoAutofix()
    expect(api.undo).not.toHaveBeenCalled()
  })

  it('opens a proposal left for review', async () => {
    await useStore.getState().reviewProposal('prop-7')
    expect(useStore.getState().proposal?.id).toBe('prop-7')
  })
})
