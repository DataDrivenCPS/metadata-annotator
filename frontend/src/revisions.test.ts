import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentRun, ModelResponse, Proposal } from './types'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })

const model = (rev: string) => ({
  head: 'rev-3', revision: { id: rev, validation: null }, info: { family: 's223' },
  view: { equipment: [], points: [], connections: [], connection_points: [], containment: [] }, issues: [],
}) as unknown as ModelResponse

vi.mock('./api', () => ({
  ApiError: class extends Error {},
  api: {
    model: vi.fn(async (_pid: string, revision?: string) => model(revision ?? 'rev-3')),
    repairs: vi.fn(async () => ({})),
    edit: vi.fn(),
    importModel: vi.fn(),
    assist: vi.fn(),
    replyToProposal: vi.fn(),
    build: vi.fn(),
    regenerate: vi.fn(),
    proposal: vi.fn(),
    applyProposal: vi.fn(),
    dismissProposal: vi.fn(),
    runs: vi.fn(async () => [] as AgentRun[]),
    sources: vi.fn(async () => []),
    views: vi.fn(async () => ({ views: [] })),
    proposals: vi.fn(async () => [] as Proposal[]),
  },
}))

const { api } = await import('./api')
const { useStore } = await import('./store')

describe('browsing earlier revisions', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useStore.setState({ projectId: 'p', model: model('rev-3'), viewing: null, selection: { entity_ids: [], relationship_ids: [], field_ids: [], source_regions: [] } })
  })

  it('loads the chosen revision and goes back to the current one', async () => {
    await useStore.getState().viewRevision('rev-1')
    expect(vi.mocked(api.model).mock.calls.at(-1)).toEqual(['p', 'rev-1'])
    expect(useStore.getState().model?.revision.id).toBe('rev-1')
    await useStore.getState().viewRevision(null)
    expect(vi.mocked(api.model).mock.calls.at(-1)).toEqual(['p', undefined])
    expect(useStore.getState().viewing).toBeNull()
  })

  it('is read-only: edits and requests are refused with a notice', async () => {
    await useStore.getState().viewRevision('rev-1')
    expect(await useStore.getState().edit([{ op: 'update_point' }])).toBe(false)
    expect(await useStore.getState().assist('fix it')).toBe(false)
    await useStore.getState().importModel({ name: 'model.ttl' } as File)
    expect(api.importModel).not.toHaveBeenCalled()
    expect(api.edit).not.toHaveBeenCalled()
    expect(api.assist).not.toHaveBeenCalled()
    expect(useStore.getState().toast?.text).toContain('read-only')
  })

  it('viewing the head is just the current model', async () => {
    await useStore.getState().viewRevision('rev-3')
    expect(useStore.getState().viewing).toBeNull()
  })
})


const deferred = <T,>() => {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

describe('request ordering and reconnect recovery', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useStore.setState({ projectId: 'p', model: model('rev-3'), viewing: null, proposal: null,
      activeRunId: null, runs: {}, autofix: null, assistantCleared: { runs: [], proposals: [] } })
  })

  it('discards a model response after switching projects', async () => {
    const pending = deferred<ModelResponse>()
    vi.mocked(api.model).mockReturnValueOnce(pending.promise)
    const reload = useStore.getState().reload()
    useStore.setState({ projectId: 'other', model: null })
    pending.resolve(model('rev-1'))
    await reload
    expect(useStore.getState().model).toBeNull()
    expect(useStore.getState().projectId).toBe('other')
  })

  it('discards an old response even after returning to the same project', async () => {
    const pending = deferred<ModelResponse>()
    vi.mocked(api.model).mockReturnValueOnce(pending.promise)
    const old = useStore.getState().reload()
    await useStore.getState().openProject(null)
    await useStore.getState().openProject('p')
    pending.resolve(model('rev-1'))
    await old
    expect(useStore.getState().model?.revision.id).toBe('rev-3')
  })

  it('discards a response for a superseded historical revision', async () => {
    const pending = deferred<ModelResponse>()
    vi.mocked(api.model).mockReturnValueOnce(pending.promise)
    const old = useStore.getState().viewRevision('rev-1')
    await useStore.getState().viewRevision('rev-2')
    pending.resolve(model('rev-1'))
    await old
    expect(useStore.getState().viewing).toBe('rev-2')
    expect(useStore.getState().model?.revision.id).toBe('rev-2')
  })

  it('keeps the newest reload when earlier requests finish later', async () => {
    const pending = deferred<ModelResponse>()
    vi.mocked(api.model).mockReturnValueOnce(pending.promise)
    const old = useStore.getState().reload()
    await useStore.getState().reload()
    pending.resolve(model('rev-1'))
    await old
    expect(useStore.getState().model?.revision.id).toBe('rev-3')
  })

  it('recovers completed runs, proposals and the current model after missed events', async () => {
    const running = { id: 'run', kind: 'autofix', status: 'running', outcome: {} } as AgentRun
    const finished = { ...running, status: 'succeeded', outcome: { autofix: { groups: [], revisions: ['rev-3'] } } } as AgentRun
    const proposal = { id: 'proposal', status: 'pending', base_revision: 'rev-3' } as Proposal
    useStore.setState({ model: model('rev-1'), runs: { run: running }, activeRunId: 'run', autofix: { runId: 'run' } })
    vi.mocked(api.runs).mockResolvedValueOnce([finished])
    vi.mocked(api.proposals).mockResolvedValueOnce([proposal])
    await useStore.getState().reconcile()
    expect(useStore.getState().model?.revision.id).toBe('rev-3')
    expect(useStore.getState().runs.run.status).toBe('succeeded')
    expect(useStore.getState().proposal?.id).toBe('proposal')
  })

  it('preserves run events received while reconnect reconciliation is pending', async () => {
    const pending = deferred<AgentRun[]>()
    vi.mocked(api.runs).mockReturnValueOnce(pending.promise)
    const refresh = useStore.getState().reconcile()
    const finished = { id: 'run', kind: 'autofix', status: 'succeeded', outcome: {} } as AgentRun
    useStore.getState().handleEvent({ type: 'run', run: finished })
    pending.resolve([{ ...finished, status: 'running' }])
    await refresh
    expect(useStore.getState().runs.run.status).toBe('succeeded')
  })
})


describe('starting assistant runs', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useStore.setState({ projectId: 'p', model: model('rev-3'), viewing: null, runs: {}, activeRunId: null,
      selection: { entity_ids: [], relationship_ids: [], field_ids: [], source_regions: [] },
      proposal: { id: 'old', status: 'pending', base_revision: 'rev-3' } as Proposal })
  })

  const start = (kind: string) => {
    if (kind === 'reply') return useStore.getState().replyToProposal('Change it')
    if (kind === 'build') return useStore.getState().startBuild(['source'], '')
    if (kind === 'regenerate') return useStore.getState().regenerate()
    return useStore.getState().assist('Add an AHU')
  }
  const endpoint = (kind: string) => kind === 'reply' ? api.replyToProposal : kind === 'build' ? api.build
    : kind === 'regenerate' ? api.regenerate : api.assist

  it.each(['assist', 'reply', 'build', 'regenerate'])('keeps %s completion received before its start response', async (kind) => {
    const pending = deferred<AgentRun>()
    vi.mocked(endpoint(kind)).mockReturnValueOnce(pending.promise)
    vi.mocked(api.proposal).mockResolvedValueOnce({ id: 'new', status: 'pending' } as Proposal)
    const started = start(kind)
    const done = { id: 'run', kind: 'correction', status: 'succeeded', outcome: { proposal_id: 'new' } } as AgentRun
    useStore.getState().handleEvent({ type: 'run', run: done })
    pending.resolve({ ...done, status: 'queued', outcome: {} })
    await started
    expect(useStore.getState().runs.run.status).toBe('succeeded')
    await vi.waitFor(() => expect(useStore.getState().proposal?.id).toBe('new'))
  })

  it('uses a completed HTTP start response when only the queued event has arrived', async () => {
    const pending = deferred<AgentRun>()
    vi.mocked(api.assist).mockReturnValueOnce(pending.promise)
    vi.mocked(api.proposal).mockResolvedValueOnce({ id: 'new', status: 'pending' } as Proposal)
    const started = start('assist')
    const done = { id: 'run', kind: 'correction', status: 'succeeded', outcome: { proposal_id: 'new' } } as AgentRun
    useStore.getState().handleEvent({ type: 'run', run: { ...done, status: 'queued', outcome: {} } })
    pending.resolve(done)
    await started
    expect(useStore.getState().runs.run.status).toBe('succeeded')
    await vi.waitFor(() => expect(useStore.getState().proposal?.id).toBe('new'))
  })

  it('keeps the latest requested run active when start responses arrive out of order', async () => {
    const pending = deferred<AgentRun>()
    vi.mocked(api.assist).mockReturnValueOnce(pending.promise)
    const first = start('assist')
    vi.mocked(api.assist).mockResolvedValueOnce({ id: 'new', kind: 'correction', status: 'queued', outcome: {} } as AgentRun)
    await start('assist')
    pending.resolve({ id: 'old', kind: 'correction', status: 'queued', outcome: {} } as AgentRun)
    await first
    expect(useStore.getState().activeRunId).toBe('new')
    expect(Object.keys(useStore.getState().runs).sort()).toEqual(['new', 'old'])
  })

  it.each(['assist', 'reply', 'build', 'regenerate'])('discards a %s start response after leaving its project', async (kind) => {
    const pending = deferred<AgentRun>()
    vi.mocked(endpoint(kind)).mockReturnValueOnce(pending.promise)
    const started = start(kind)
    await useStore.getState().openProject(null)
    pending.resolve({ id: 'run', kind: 'correction', status: 'queued', outcome: {} } as AgentRun)
    await started
    expect(useStore.getState().runs).toEqual({})
    expect(useStore.getState().activeRunId).toBeNull()
  })
})


describe('proposal response ownership', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useStore.setState({ projectId: 'p', model: model('rev-3'), viewing: null, proposalStates: {}, toast: null, busy: false,
      proposal: { id: 'old', status: 'pending', operations: [] } as unknown as Proposal })
  })

  it('does not restore an applied proposal after leaving its project', async () => {
    const pending = deferred<Awaited<ReturnType<typeof api.applyProposal>>>()
    vi.mocked(api.applyProposal).mockReturnValueOnce(pending.promise)
    const applying = useStore.getState().applyProposal()
    await useStore.getState().openProject(null)
    pending.resolve({ id: 'rev-4' } as Awaited<ReturnType<typeof api.applyProposal>>)
    await applying
    expect(useStore.getState().proposal).toBeNull()
    expect(useStore.getState().proposalStates).toEqual({})
    expect(useStore.getState().busy).toBe(false)
  })

  it('does not erase a newly selected proposal when dismissal finishes', async () => {
    const pending = deferred<Awaited<ReturnType<typeof api.dismissProposal>>>()
    vi.mocked(api.dismissProposal).mockReturnValueOnce(pending.promise)
    const dismissing = useStore.getState().dismissProposal()
    useStore.setState({ proposal: { id: 'new', status: 'pending' } as Proposal })
    pending.resolve({} as Awaited<ReturnType<typeof api.dismissProposal>>)
    await dismissing
    expect(useStore.getState().proposal?.id).toBe('new')
    expect(useStore.getState().proposalStates.old).toBe('dismissed')
  })

  it('does not install a proposal requested in a project that has been closed', async () => {
    const pending = deferred<Proposal>()
    vi.mocked(api.proposal).mockReturnValueOnce(pending.promise)
    const reviewing = useStore.getState().reviewProposal('old')
    await useStore.getState().openProject(null)
    pending.resolve({ id: 'old', status: 'pending' } as Proposal)
    await reviewing
    expect(useStore.getState().proposal).toBeNull()
    expect(useStore.getState().proposalStates).toEqual({})
  })
})
