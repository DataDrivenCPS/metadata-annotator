import type {
  AgentRun, IssueRepair, Observation, CsvGrid, CsvImportConfig, CsvPreview, ProviderHealth, Source, EntityDetail, EntityRelations, ModelResponse, ViewData, ViewInfo, ProjectInfo, Proposal, Revision, Selection, Status, Term,
} from './types'

export class ApiError extends Error {
  status: number
  body: Record<string, unknown>
  constructor(status: number, body: Record<string, unknown>) {
    super(String(body.detail ?? body.error ?? `HTTP ${status}`))
    this.status = status
    this.body = body
  }
  get isStale() { return this.status === 409 && this.body.error === 'stale' }
}

async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, headers: {} }
  if (body instanceof FormData) init.body = body
  else if (body !== undefined) {
    init.body = JSON.stringify(body)
    ;(init.headers as Record<string, string>)['Content-Type'] = 'application/json'
  }
  const res = await fetch(url, init)
  const text = await res.text()
  let data: unknown = null
  try { data = text ? JSON.parse(text) : null } catch { data = { detail: text } }
  if (!res.ok) {
    const b = (data && typeof data === 'object' ? data : { detail: String(data) }) as Record<string, unknown>
    if (typeof b.detail !== 'string' && b.detail !== undefined) b.detail = JSON.stringify(b.detail)
    throw new ApiError(res.status, b)
  }
  return data as T
}

const P = (pid: string) => `/api/projects/${encodeURIComponent(pid)}`

export const api = {
  status: () => req<Status>('GET', '/api/status'),
  providerHealth: (name: string) =>
    req<ProviderHealth>('GET', `/api/providers/${encodeURIComponent(name)}/health`),
  projects: () => req<ProjectInfo[]>('GET', '/api/projects'),
  createProject: (name: string, profile: string) => req<ProjectInfo>('POST', '/api/projects', { name, profile }),
  createSample: () => req<ProjectInfo>('POST', '/api/projects/sample'),
  model: (pid: string, revision?: string) =>
    req<ModelResponse>('GET', `${P(pid)}/model${revision ? `?revision=${revision}` : ''}`),
  revisions: (pid: string) => req<Revision[]>('GET', `${P(pid)}/revisions`),
  importModel: (pid: string, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return req<Revision>('POST', `${P(pid)}/import`, fd)
  },
  edit: (pid: string, base_revision: string, operations: Record<string, unknown>[], summary?: string) =>
    req<{ revision: Revision; notes: string[] }>('POST', `${P(pid)}/edits`, { base_revision, operations, summary }),
  undo: (pid: string) => req<{ head: string }>('POST', `${P(pid)}/undo`),
  redo: (pid: string) => req<{ head: string }>('POST', `${P(pid)}/redo`),
  saveLayout: (pid: string, positions: Record<string, [number, number]>) =>
    req('PUT', `${P(pid)}/layout`, { positions }),
  repairs: (pid: string, revision: string) =>
    req<Record<string, IssueRepair>>('GET', `${P(pid)}/repairs?revision=${encodeURIComponent(revision)}`),
  setIssueState: (pid: string, id: string, state: 'open' | 'dismissed') =>
    req('PUT', `${P(pid)}/issues/${encodeURIComponent(id)}`, { state }),
  views: (pid: string) => req<{ views: ViewInfo[]; errors: string[] }>('GET', `${P(pid)}/views`),
  view: (pid: string, vid: string, revision?: string) =>
    req<ViewData>('GET', `${P(pid)}/views/${vid}${revision ? `?revision=${revision}` : ''}`),
  entityRelations: (pid: string, eid: string, revision?: string) =>
    req<EntityRelations>('GET', `${P(pid)}/entities/${eid}/relations${revision ? `?revision=${revision}` : ''}`),
  entity: (pid: string, eid: string, revision?: string) =>
    req<EntityDetail>('GET', `${P(pid)}/entities/${eid}${revision ? `?revision=${revision}` : ''}`),
  describeSelection: (pid: string, sel: Selection) =>
    req<{ summary: string }>('POST', `${P(pid)}/selection/describe`, sel),
  assist: (pid: string, base_revision: string, selection: Selection, instruction: string, provider?: string,
    parent_run_id?: string) =>
    req<AgentRun>('POST', `${P(pid)}/assist`, { base_revision, selection, instruction, provider, parent_run_id }),
  autofix: (pid: string, base_revision: string, issue_ids: string[], provider?: string) =>
    req<AgentRun>('POST', `${P(pid)}/autofix`, { base_revision, issue_ids, provider }),
  build: (pid: string, base_revision: string, source_ids: string[], instruction: string, provider?: string, source_pages?: Record<string, number[]>) =>
    req<AgentRun>('POST', `${P(pid)}/build`, { base_revision, source_ids, instruction, provider, source_pages }),
  run: (pid: string, id: string) => req<AgentRun>('GET', `${P(pid)}/runs/${id}`),
  runs: (pid: string) => req<AgentRun[]>('GET', `${P(pid)}/runs`),
  cancelRun: (pid: string, id: string) => req<AgentRun>('POST', `${P(pid)}/runs/${id}/cancel`),
  proposal: (pid: string, id: string) => req<Proposal>('GET', `${P(pid)}/proposals/${id}`),
  proposals: (pid: string) => req<Proposal[]>('GET', `${P(pid)}/proposals`),
  replyToProposal: (pid: string, id: string, instruction: string, provider?: string, parent_run_id?: string) =>
    req<AgentRun>('POST', `${P(pid)}/proposals/${id}/reply`, { instruction, provider, parent_run_id }),
  applyProposal: (pid: string, id: string) => req<Revision>('POST', `${P(pid)}/proposals/${id}/apply`),
  dismissProposal: (pid: string, id: string) => req<Proposal>('POST', `${P(pid)}/proposals/${id}/dismiss`),
  regenerate: (pid: string, id: string, provider?: string) =>
    req<AgentRun>('POST', `${P(pid)}/proposals/${id}/regenerate`, { provider }),
  searchTerms: (q: string, kind: string | undefined, profile: string) =>
    req<Term[]>('GET', `/api/vocabulary/search?q=${encodeURIComponent(q)}${kind ? `&kind=${kind}` : ''}&limit=25&profile=${profile}`),
  options: (kind: string, quantityKind: string | undefined | null, profile: string) =>
    req<Term[]>('GET', `/api/vocabulary/options/${kind}?profile=${profile}${quantityKind ? `&quantity_kind=${encodeURIComponent(quantityKind)}` : ''}`),
  sources: (pid: string) => req<Source[]>('GET', `${P(pid)}/sources`),
  uploadSource: (pid: string, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return req<Source>('POST', `${P(pid)}/sources`, fd)
  },
  documentPreview: (pid: string, sid: string, page = 1) => req<{ text: string; truncated: boolean }>('GET', `${P(pid)}/sources/${sid}/document-preview?page=${page}`),
  sourcePageUrl: (pid: string, sid: string, page: number) => `${P(pid)}/sources/${sid}/pages/${page}.png`,
  sourceFileUrl: (pid: string, sid: string) => `${P(pid)}/sources/${sid}/file`,
  sourceGrid: (pid: string, sid: string) => req<CsvGrid>('GET', `${P(pid)}/sources/${sid}/grid?limit=80`),
  sourcePreview: (pid: string, sid: string, cfg: CsvImportConfig) => req<CsvPreview>('POST', `${P(pid)}/sources/${sid}/preview`, cfg),
  sourceConfirm: (pid: string, sid: string, cfg: CsvImportConfig) =>
    req<{ observations: number; superseded: number }>('POST', `${P(pid)}/sources/${sid}/confirm`, cfg),
  observations: (pid: string, sid: string) => req<Observation[]>('GET', `${P(pid)}/sources/${sid}/observations?limit=5000`),
  exportUrl: (pid: string, revision: string, kind: 'ttl' | 'csv') =>
    `${P(pid)}/${kind === 'ttl' ? 'export.ttl' : 'export-points.csv'}?revision=${revision}`,
  eventsUrl: (pid: string) => `${P(pid)}/events`,
}
