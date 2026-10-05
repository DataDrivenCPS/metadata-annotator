import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentRun, ModelResponse, Proposal, ReviewIssue } from './types'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {} })

// The model the "server" returns; tests change which issues are open.
let openIds: string[] = []
const issue = (id: string, affected = `eq-${id}`): ReviewIssue => ({
  id, affected_ids: [affected], category: 'validation', severity: 'violation', explanation: `problem ${id}`,
  resolution_state: 'open', origin: 'validation', details: {},
})
const model = () => ({
  head: 'rev-1', info: { family: 's223' },
  view: { equipment: [], points: [], connections: [], connection_points: [], containment: [] },
  issues: openIds.map((id) => issue(id)), revision: { validation: null },
}) as unknown as ModelResponse
let runCount = 0
const run = (id: string, status: AgentRun['status'], proposal_id: string | null = null): AgentRun => ({
  id, kind: 'correction', input_revision: 'rev-1', selection: null, instruction: '', provider: 'p', model: 'm',
  skill_version: '', status, progress: [], outcome: { proposal_id, dismissed_proposal_id: null, explanation: '' },
  error: null, created_at: '', finished_at: '',
})

vi.mock('./api', () => ({
  ApiError: class extends Error {},
  api: {
    model: vi.fn(async () => model()),
    repairs: vi.fn(async () => ({})),
    assist: vi.fn(async () => run(`run-${++runCount}`, 'queued')),
    cancelRun: vi.fn(async (_pid: string, id: string) => run(id, 'cancelled')),
    proposal: vi.fn(async (_pid: string, id: string) => ({ id, status: 'applied' })),
    applyProposal: vi.fn(async () => ({ id: 'rev-2' })),
    dismissProposal: vi.fn(async () => ({})),
  },
}))

const { api } = await import('./api')
const { useStore } = await import('./store')

const proposalFor = (runId: string, id = 'prop-1'): Proposal => ({
  id, agent_run_id: runId, status: 'pending', operations: [{ op: 'update_point' }], issue_dismissals: [],
}) as unknown as Proposal

describe('auto-fix', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    runCount = 0
    openIds = ['a', 'b', 'c']
    useStore.setState({ projectId: 'p', model: model(), rows: new Map(), runs: {}, proposal: null, autofix: null, activeRunId: null })
  })

  it('asks about one issue at a time and waits for review', async () => {
    const s = useStore.getState()
    await s.startAutofix(model().issues)
    expect(api.assist).toHaveBeenCalledTimes(1)
    expect(vi.mocked(api.assist).mock.calls[0][3]).toContain('[a]')
    let af = useStore.getState().autofix!
    expect(af.current?.id).toBe('a')
    expect(af.paused).toBeNull()
    useStore.getState().handleEvent({ type: 'run', run: run('run-1', 'succeeded', 'prop-1') })
    af = useStore.getState().autofix!
    expect(af.paused).toBe('review')
  })

  it('applying moves on, skipping issues the fix already resolved', async () => {
    await useStore.getState().startAutofix(model().issues)
    useStore.setState({ proposal: proposalFor('run-1') })
    openIds = ['c']  // the fix for a also resolved b
    await useStore.getState().applyProposal()
    const af = useStore.getState().autofix!
    expect(af.results.map((r) => [r.id, r.outcome])).toEqual([['a', 'applied'], ['b', 'resolved']])
    expect(af.current?.id).toBe('c')
    expect(vi.mocked(api.assist).mock.calls[1][3]).toContain('[c]')
  })

  it('needs input when the run ends without a proposal; skip moves on', async () => {
    await useStore.getState().startAutofix(model().issues)
    useStore.getState().handleEvent({ type: 'run', run: run('run-1', 'succeeded') })
    expect(useStore.getState().autofix!.paused).toBe('input')
    await useStore.getState().skipAutofixIssue()
    const af = useStore.getState().autofix!
    expect(af.results).toEqual([{ id: 'a', explanation: 'problem a', outcome: 'skipped' }])
    expect(af.current?.id).toBe('b')
  })

  it('discarding a proposal counts as a skip; the end shows a summary; stop clears it', async () => {
    openIds = ['a']
    await useStore.getState().startAutofix(model().issues)
    useStore.setState({ proposal: proposalFor('run-1') })
    await useStore.getState().dismissProposal()
    const af = useStore.getState().autofix!
    expect(af.current).toBeNull()
    expect(af.results.map((r) => r.outcome)).toEqual(['skipped'])
    useStore.getState().stopAutofix()
    expect(useStore.getState().autofix).toBeNull()
  })

  it('ignores proposals from other conversations', async () => {
    await useStore.getState().startAutofix(model().issues)
    useStore.setState({ proposal: proposalFor('some-other-run') })
    await useStore.getState().applyProposal()
    expect(useStore.getState().autofix!.current?.id).toBe('a')
    expect(api.assist).toHaveBeenCalledTimes(1)
  })
})
