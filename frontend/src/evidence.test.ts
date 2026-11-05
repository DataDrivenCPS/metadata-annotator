import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentRun, ModelResponse, Selection, SourceRegion } from './types'
import { attachmentSummary } from './documents'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })

const model = { head: 'rev-3', revision: { id: 'rev-3', validation: null }, info: { family: 's223' },
  view: { equipment: [], points: [], connections: [], connection_points: [], containment: [] }, issues: [],
} as unknown as ModelResponse

vi.mock('./api', () => ({
  ApiError: class extends Error {},
  api: { assist: vi.fn(), replyToProposal: vi.fn(), build: vi.fn(), proposal: vi.fn() },
}))

const { api } = await import('./api')
const { useStore } = await import('./store')

const pdf: SourceRegion = { source_id: 'src-pdf', role: 'reference', pages: [1, 2, 3] }
const notes: SourceRegion = { source_id: 'src-notes', role: 'reference' }
const selection = (regions: SourceRegion[]): Selection =>
  ({ entity_ids: ['eq-1'], relationship_ids: [], field_ids: [], source_regions: regions })
const started = (kind: AgentRun['kind'], regions: SourceRegion[]) => ({
  id: `run-${kind}`, kind, status: 'queued', outcome: {}, selection: selection(regions),
}) as unknown as AgentRun

describe('attached evidence belongs to one message', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useStore.setState({ projectId: 'p', model, viewing: null, runs: {}, activeRunId: null, proposal: null,
      selection: selection([pdf, notes]), selectionVersion: 4 })
  })

  it('leaves the composer once the message that carried it starts a run', async () => {
    vi.mocked(api.assist).mockResolvedValueOnce(started('correction', [pdf, notes]))
    await useStore.getState().assist('Which pump feeds RO-1?')
    expect(vi.mocked(api.assist).mock.calls[0][2].source_regions).toEqual([pdf, notes])
    const state = useStore.getState()
    expect(state.selection.source_regions).toEqual([])
    expect(state.selection.entity_ids).toEqual(['eq-1'])  // the objects stay selected
    expect(state.selectionVersion).toBe(4)  // detaching is not a new pick
  })

  it('keeps evidence that the started run did not carry', async () => {
    vi.mocked(api.assist).mockResolvedValueOnce(started('correction', [pdf]))
    await useStore.getState().assist('Which pump feeds RO-1?')
    expect(useStore.getState().selection.source_regions).toEqual([notes])
  })

  it('does not detach attachments when a build starts from the sources pane', async () => {
    vi.mocked(api.build).mockResolvedValueOnce(started('build', [pdf]))
    await useStore.getState().startBuild(['src-pdf'], '')
    expect(useStore.getState().selection.source_regions).toEqual([pdf, notes])
  })
})

describe('attachment summary', () => {
  it('counts sources and chosen pages', () => {
    expect(attachmentSummary([pdf, notes])).toBe('2 sources, 3 pages')
    expect(attachmentSummary([notes])).toBe('1 source')
    expect(attachmentSummary([{ source_id: 'a', pages: [4] }, { source_id: 'a', pages: [7] }])).toBe('1 source, 2 pages')
  })
})
