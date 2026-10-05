import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ModelResponse } from './types'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {} })

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
    assist: vi.fn(),
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
    expect(api.edit).not.toHaveBeenCalled()
    expect(api.assist).not.toHaveBeenCalled()
    expect(useStore.getState().toast?.text).toContain('read-only')
  })

  it('viewing the head is just the current model', async () => {
    await useStore.getState().viewRevision('rev-3')
    expect(useStore.getState().viewing).toBeNull()
  })
})
