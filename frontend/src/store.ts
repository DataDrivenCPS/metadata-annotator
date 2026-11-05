import { create } from 'zustand'
import { api, ApiError } from './api'
import {
  autofixReport, continuation, effectiveSelection, isActive, issueSelection, issuesPrompt, openEdits, proposalStates, restorableProposal,
  siblingOptions, threadRuns, type AutofixState, type ProposalState,
} from './assistant'
import { applyClick, pruneSelection, type ClickTarget, type Modifiers } from './selection'
import { providerOf } from './providers'
import {
  emptySelection, type AgentRun, type IssueRepair, type ViewInfo, type ModelResponse, type Proposal, type ReviewIssue, type Row, type Selection, type SourceRole, type Status,
} from './types'

export type Tab = 'points' | 'equipment' | 'spaces' | 'connections' | 'connection_points' | 'graph' | 'issues' | 'pending' | `view:${string}`
export type DrawerTab = 'inspector' | 'rdf' | 'history'

interface Toast { kind: 'info' | 'error' | 'success'; text: string; action?: { label: string; run: () => void } }

interface State {
  status: Status | null
  projectId: string | null
  model: ModelResponse | null
  rows: Map<string, Row>
  selection: Selection
  anchor: string | null
  tab: Tab
  drawerTab: DrawerTab
  inspectId: string | null
  runs: Record<string, AgentRun>
  activeRunId: string | null
  proposal: Proposal | null
  proposalStates: Record<string, ProposalState>
  /** Every proposal still waiting for the person (chat, auto-fix, builds), newest first: the Pending edits tab. */
  pendingEdits: Proposal[]
  /** The card the Pending edits tab scrolls to and highlights (set by links from the chat). */
  pendingFocus: string | null
  /** Pending edits cards folded to their header (by the card's first proposal id). */
  pendingCollapsed: string[]
  togglePending: (cardId: string) => void
  /** Repair-engine detail per issue id, for the revision named (loaded in the background). */
  repairs: { revision: string; byIssue: Record<string, IssueRepair> } | null
  assistantDraft: string
  /** A message on its way to the server, shown in the thread until its run arrives. */
  assistantSending: string | null
  assistantIssueContext: { issues: ReviewIssue[]; text: string } | null
  assistantDraftVersion: number
  /** The next message starts a new request instead of continuing the conversation. */
  assistantNewRequest: boolean
  /** The model changed since the reply being continued, and the person chose to start a fresh model
   *  conversation with the current model instead of continuing it (reset after each message). */
  assistantFresh: boolean
  setAssistantFresh: (fresh: boolean) => void
  /** Counts the person's selection changes; with runSelectionVersion it tells a selection made since a request from a leftover one. */
  selectionVersion: number
  runSelectionVersion: Record<string, number>
  /** The person removed the conversation's own scope: the next message is about nothing in particular. */
  assistantNoScope: boolean
  dropConversationScope: () => void
  /** Conversation items cleared locally, retained on the server for project history. */
  assistantCleared: { runs: string[]; proposals: string[] }
  provider: string | null
  /** Models each provider's server listed at its last health check (providers without a configured model). */
  providerModels: Record<string, string[]>
  /** Whether each checked model choice reads images, as its server said (providers that do not configure it). */
  modelVision: Record<string, boolean>
  toast: Toast | null
  busy: boolean
  sourcesVersion: number
  sourcesOpen: boolean
  /** The auto-fix run being followed: obvious fixes are applied, the rest wait for the person. */
  autofix: AutofixState | null
  /** An earlier revision being browsed read-only; null = the current model. */
  viewing: string | null
  /** Table views for the project (curated + workbench.toml); builtin ones extend a typed table. */
  views: ViewInfo[]

  toggleSources: () => void
  loadStatus: () => Promise<void>
  openProject: (id: string | null) => Promise<void>
  reload: () => Promise<void>
  reconcile: () => Promise<void>
  click: (target: ClickTarget, mods: Modifiers, order: string[]) => void
  setSelection: (sel: Selection) => void
  clearSelection: () => void
  setTab: (tab: Tab) => void
  setDrawerTab: (tab: DrawerTab) => void
  setAssistantDraft: (text: string) => void
  setAssistantNewRequest: (newRequest: boolean) => void
  clearChat: () => void
  focusAssistant: () => void
  appendAssistantContext: (text: string) => void
  /** Select what the issues affect and add them to the assistant prompt for review. */
  addIssuesToPrompt: (issues: ReviewIssue[]) => void
  inspect: (id: string | null) => void
  edit: (ops: Record<string, unknown>[], summary?: string) => Promise<boolean>
  importModel: (file: File) => Promise<void>
  undo: () => Promise<void>
  redo: () => Promise<void>
  sendMessage: (text: string) => Promise<boolean>
  assist: (instruction: string, parentRunId?: string) => Promise<boolean>
  replyToProposal: (instruction: string, parentRunId?: string, retryLeftOut?: boolean, proposal?: Proposal) => Promise<boolean>
  startBuild: (sourceIds: string[], instruction: string, sourcePages?: Record<string, number[]>, sourceRoles?: Record<string, SourceRole>) => Promise<boolean>
  cancelRun: () => Promise<void>
  /** Apply, discard or redo a proposal: the one given (from the Pending edits tab), else the one open in the panel. */
  applyProposal: (proposal?: Proposal) => Promise<void>
  dismissProposal: (proposal?: Proposal) => Promise<void>
  regenerate: (proposal?: Proposal) => Promise<void>
  /** Refetch the proposals still waiting (after the model or a run changed). */
  loadPending: () => Promise<void>
  setProvider: (p: string) => void
  setProviderModels: (name: string, models: string[]) => void
  setModelVision: (choice: string, readsImages: boolean) => void
  handleEvent: (ev: { type: string; head?: string; summary?: string; run?: AgentRun }) => void
  notify: (t: Toast | null) => void
  startAutofix: (issues: ReviewIssue[]) => Promise<void>
  /** Cancel a running auto-fix, or close the finished report. */
  stopAutofix: () => Promise<void>
  /** Undo the automatic revisions, newest first, while they are still the newest. */
  undoAutofix: () => Promise<void>
  /** Open the Pending edits tab, on one proposal's card if given (proposals are reviewed only there). */
  showPending: (proposalId?: string) => void
  viewRevision: (revision: string | null) => Promise<void>
}

const indexRows = (m: ModelResponse | null) => {
  const map = new Map<string, Row>()
  if (!m) return map
  for (const r of [...m.view.equipment, ...m.view.points, ...m.view.connections, ...(m.view.connection_points ?? []), ...(m.view.spaces ?? []),
    ...(m.view.entities ?? []), ...(m.view.relationships ?? [])]) map.set(r.id, r)
  return map
}

const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e))

const clearedChat = (projectId: string | null): State['assistantCleared'] => {
  try {
    const saved = projectId ? JSON.parse(localStorage.getItem(`workbench.clearedChat.${projectId}`) ?? 'null') : null
    if (Array.isArray(saved?.runs) && saved.runs.every((id: unknown) => typeof id === 'string')
        && Array.isArray(saved?.proposals) && saved.proposals.every((id: unknown) => typeof id === 'string')) return saved
  } catch { /* Ignore unavailable storage or an invalid saved value. */ }
  return { runs: [], proposals: [] }
}

let projectGeneration = 0
let modelGeneration = 0
let reconciliationGeneration = 0
let assistantGeneration = 0
let autofixGeneration = 0
let pendingGeneration = 0

function latestStartedRun(run: AgentRun, runs: Record<string, AgentRun>) {
  const existing = runs[run.id]
  return existing && (!isActive(existing) || run.status === 'queued'
    || (existing.status === 'running' && run.status === 'running')) ? existing : run
}

/** The event stream can advance a run before its HTTP start response arrives. */
/**
 * Attached evidence belongs to the message it was sent with: once a message starts a run, the
 * sources it carried leave the composer, so the next message does not read them again unless the
 * person attaches them again. Not a selection change (it must not count as picking something).
 */
function detachSentEvidence(run: AgentRun) {
  if (run.kind !== 'correction') return
  const sent = new Set((run.selection?.source_regions ?? []).map((r) => r.source_id))
  const { selection } = useStore.getState()
  if (!sent.size || !selection.source_regions.some((r) => sent.has(r.source_id))) return
  useStore.setState({ selection: { ...selection, source_regions: selection.source_regions.filter((r) => !sent.has(r.source_id)) } })
}

function acceptStartedRun(run: AgentRun, pid: string, project: number, request: number, clearProposal = false) {
  const state = useStore.getState()
  if (project !== projectGeneration || state.projectId !== pid) return false
  detachSentEvidence(run)
  const latest = latestStartedRun(run, state.runs)
  if (request !== assistantGeneration) {
    useStore.setState({ runs: { ...state.runs, [run.id]: latest } })
    return true
  }
  useStore.setState({ runs: { ...state.runs, [run.id]: latest }, activeRunId: run.id,
    assistantNewRequest: false, assistantNoScope: false, ...(clearProposal ? { proposal: null } : {}) })
  // Completion may have arrived before this became the active run; recover its proposal.
  useStore.getState().handleEvent({ type: 'run', run: latest })
  return true
}

export const useStore = create<State>((set, get) => ({
  status: null,
  projectId: null,
  model: null,
  rows: new Map(),
  selection: emptySelection(),
  anchor: null,
  tab: 'points',
  drawerTab: 'inspector',
  inspectId: null,
  runs: {},
  activeRunId: null,
  proposal: null,
  proposalStates: {},
  pendingEdits: [],
  pendingFocus: null,
  pendingCollapsed: [],
  repairs: null,
  assistantDraft: '',
  assistantSending: null,
  assistantIssueContext: null,
  assistantDraftVersion: 0,
  assistantNewRequest: false,
  assistantFresh: false,
  selectionVersion: 0,
  runSelectionVersion: {},
  assistantNoScope: false,
  assistantCleared: { runs: [], proposals: [] },
  provider: null,
  providerModels: {},
  modelVision: {},
  toast: null,
  busy: false,
  sourcesVersion: 0,
  sourcesOpen: false,
  autofix: null,
  viewing: null,
  views: [],

  // Remembered per project once toggled; until then the tray opens only if the project has sources.
  toggleSources: () => {
    const sourcesOpen = !get().sourcesOpen
    const pid = get().projectId
    if (pid) localStorage.setItem(`workbench.sourcesOpen.${pid}`, String(sourcesOpen))
    set({ sourcesOpen })
  },

  loadStatus: async () => {
    const status = await api.status()
    const def = status.providers.find((p) => p.default)
    // The choice made in the dropdown survives reloads while that provider is still configured.
    const saved = localStorage.getItem('workbench.provider')
    const remembered = saved && providerOf(saved, status.providers) ? saved : null
    set({ status, provider: get().provider ?? remembered ?? def?.name ?? null })
  },

  openProject: async (id) => {
    const generation = ++projectGeneration
    ++modelGeneration
    set({ projectId: id, model: null, rows: new Map(), selection: emptySelection(), proposal: null, proposalStates: {}, pendingEdits: [], repairs: null,
          runs: {}, activeRunId: null, inspectId: null, drawerTab: 'inspector', assistantDraft: '', assistantSending: null, assistantIssueContext: null, assistantNewRequest: false, assistantFresh: false,
          assistantCleared: clearedChat(id),
          autofix: null, viewing: null, views: [], busy: false })
    if (id) {
      localStorage.setItem('workbench.project', id)
      const stored = localStorage.getItem(`workbench.sourcesOpen.${id}`)
      set({ sourcesOpen: stored === 'true' })
      if (stored === null) void api.sources(id).then((list) => {
        if (generation === projectGeneration && get().projectId === id && localStorage.getItem(`workbench.sourcesOpen.${id}`) === null) {
          set({ sourcesOpen: list.length > 0 })
        }
      }).catch(() => {})
      await get().reload()
      if (generation !== projectGeneration) return
      void api.views(id).then((r) => { if (generation === projectGeneration && get().projectId === id) set({ views: r.views }) }).catch(() => {})
      // restore the conversation and the latest pending proposal, if any
      const [proposals, runs] = await Promise.all([api.proposals(id), api.runs(id).catch(() => [] as AgentRun[])])
      if (get().projectId !== id || generation !== projectGeneration) return
      const merged = { ...Object.fromEntries(runs.map((r) => [r.id, r])), ...get().runs }
      const cleared = get().assistantCleared
      const thread = threadRuns(merged, cleared.runs)
      const autofixing = Object.values(merged).find((r) => r.kind === 'autofix' && isActive(r))
      set({ runs: merged, proposalStates: proposalStates(proposals), pendingEdits: openEdits(proposals),
            activeRunId: get().activeRunId ?? thread[thread.length - 1]?.id ?? null,
            autofix: get().autofix ?? (autofixing ? { runId: autofixing.id } : null) })
      const pending = restorableProposal(proposals, thread, cleared.proposals, get().proposal?.id)
      if (pending) set({ proposal: pending })
    } else {
      localStorage.removeItem('workbench.project')
    }
  },

  reload: async () => {
    const pid = get().projectId
    if (!pid) return
    const viewing = get().viewing
    const generation = ++modelGeneration
    const project = projectGeneration
    const model = await api.model(pid, viewing ?? undefined)
    if (generation !== modelGeneration || project !== projectGeneration
        || get().projectId !== pid || get().viewing !== viewing) return
    if (viewing && viewing === model.head) set({ viewing: null })  // browsing the head is just the current model
    const rows = indexRows(model)
    const live = new Set(rows.keys())
    let proposal = get().proposal
    if (proposal && proposal.status === 'pending' && proposal.base_revision !== model.head) {
      proposal = { ...proposal, status: 'stale' }
    }
    set({ model, rows, selection: pruneSelection(get().selection, live), proposal,
          inspectId: get().inspectId && live.has(get().inspectId!) ? get().inspectId : null })
    if (get().repairs?.revision !== model.revision.id) {
      const revision = model.revision.id
      void api.repairs(pid, revision).then((byIssue) => {
        if (project === projectGeneration && get().projectId === pid && get().model?.revision.id === revision) set({ repairs: { revision, byIssue } })
      }).catch(() => {})
    }
  },

  reconcile: async () => {
    const pid = get().projectId
    if (!pid) return
    const project = projectGeneration
    const generation = ++reconciliationGeneration
    const initialRuns = get().runs
    const [, runs, proposals] = await Promise.all([get().reload(), api.runs(pid), api.proposals(pid)])
    if (project !== projectGeneration || generation !== reconciliationGeneration || get().projectId !== pid) return
    // Events received during the fetch are newer than its snapshot.
    const updates = Object.fromEntries(Object.entries(get().runs).filter(([id, run]) => initialRuns[id] !== run))
    const merged = { ...Object.fromEntries(runs.map((r) => [r.id, r])), ...updates }
    const cleared = get().assistantCleared
    const thread = threadRuns(merged, cleared.runs)
    const activeRunId = get().activeRunId ?? thread[thread.length - 1]?.id ?? null
    let proposal = restorableProposal(proposals, thread, cleared.proposals, get().proposal?.id)
    if (proposal?.status === 'pending' && get().model && proposal.base_revision !== get().model!.head) {
      proposal = { ...proposal, status: 'stale' }
    }
    const autofixing = Object.values(merged).find((r) => r.kind === 'autofix' && isActive(r))
    set({ runs: merged, activeRunId, proposal, proposalStates: proposalStates(proposals), pendingEdits: openEdits(proposals),
          sourcesVersion: get().sourcesVersion + 1,
          autofix: get().autofix ?? (autofixing ? { runId: autofixing.id } : null) })
  },

  click: (target, mods, order) => {
    const selection = applyClick(get().selection, target, mods, order, get().anchor)
    set({ selection, anchor: mods.shift ? get().anchor : target.id, inspectId: target.id, selectionVersion: get().selectionVersion + 1, assistantNoScope: false })
  },
  setSelection: (selection) => set({ selection, selectionVersion: get().selectionVersion + 1, assistantNoScope: false }),
  clearSelection: () => set((s) => ({ selection: emptySelection(), anchor: null, selectionVersion: s.selectionVersion + 1 })),
  dropConversationScope: () => set({ assistantNoScope: true }),
  setTab: (tab) => set({ tab }),
  setDrawerTab: (drawerTab) => set({ drawerTab }),
  setAssistantDraft: (assistantDraft) => set((s) => ({ assistantDraft,
    assistantIssueContext: s.assistantIssueContext && assistantDraft.includes(s.assistantIssueContext.text)
      ? s.assistantIssueContext : null })),
  setAssistantNewRequest: (assistantNewRequest) => set({ assistantNewRequest }),
  setAssistantFresh: (assistantFresh) => set({ assistantFresh }),
  clearChat: () => {
    const s = get()
    const thread = threadRuns(s.runs)
    if (s.busy || thread.some(isActive)) return
    const assistantCleared = {
      runs: [...new Set([...s.assistantCleared.runs, ...thread.map((r) => r.id)])],
      proposals: [...new Set([...s.assistantCleared.proposals,
        ...thread.flatMap((r) => r.outcome.proposal_id ? [r.outcome.proposal_id] : []),
        ...(s.proposal ? [s.proposal.id] : [])])],
    }
    if (s.projectId) {
      try { localStorage.setItem(`workbench.clearedChat.${s.projectId}`, JSON.stringify(assistantCleared)) }
      catch { /* Clearing still works for this session if storage is unavailable. */ }
    }
    set({ assistantCleared, activeRunId: null, proposal: null, assistantDraft: '', assistantIssueContext: null, assistantNewRequest: true })
  },
  focusAssistant: () => set((s) => ({ assistantNewRequest: false, assistantDraftVersion: s.assistantDraftVersion + 1 })),
  appendAssistantContext: (context) => set((s) => ({
    assistantDraft: s.assistantDraft.trim() ? `${s.assistantDraft.trim()}\n\n${context}` : context,
    assistantDraftVersion: s.assistantDraftVersion + 1,
    assistantNewRequest: true,
  })),
  addIssuesToPrompt: (issues) => {
    if (!issues.length) return
    const { rows, assistantDraft, assistantIssueContext } = get()
    const previous = assistantIssueContext && assistantDraft.includes(assistantIssueContext.text) ? assistantIssueContext : null
    const merged = [...new Map([...(previous?.issues ?? []), ...issues].map((issue) => [issue.id, issue])).values()]
    const text = issuesPrompt(merged, rows)
    set((s) => ({ selection: issueSelection(merged, rows), selectionVersion: s.selectionVersion + 1, assistantNoScope: false,
      assistantDraft: previous ? assistantDraft.replace(previous.text, () => text)
        : [assistantDraft.trim(), text].filter(Boolean).join('\n\n'),
      assistantIssueContext: { issues: merged, text }, assistantNewRequest: true,
      assistantDraftVersion: s.assistantDraftVersion + 1,
    }))
  },
  inspect: (inspectId) => set({ inspectId }),

  importModel: async (file) => {
    if (readOnly()) return
    const pid = get().projectId
    if (!pid) return
    const project = projectGeneration
    try {
      const revision = await api.importModel(pid, file)
      if (project !== projectGeneration || get().projectId !== pid) return
      await get().reload()
      get().notify({ kind: 'success', text: revision.summary })
    } catch (err) {
      if (project === projectGeneration && get().projectId === pid) {
        get().notify({ kind: 'error', text: `Import failed: ${errorText(err)}` })
      }
    }
  },

  edit: async (ops, summary) => {
    if (readOnly()) return false
    const { projectId, model } = get()
    if (!projectId || !model) return false
    set({ busy: true })
    try {
      const res = await api.edit(projectId, model.head, ops, summary)
      await get().reload()
      get().notify({ kind: 'success', text: `Saved as ${res.revision.id}: ${res.revision.summary}`
        + (res.notes.length ? ` — note: ${res.notes.join('; ')}` : ''),
        action: { label: 'Undo', run: () => void get().undo() } })
      return true
    } catch (e) {
      if (e instanceof ApiError && e.isStale) {
        await get().reload()
        get().notify({ kind: 'error', text: 'The model changed before your edit was saved. Please redo the edit.' })
      } else get().notify({ kind: 'error', text: `Edit not saved: ${errorText(e)}` })
      return false
    } finally {
      set({ busy: false })
    }
  },

  undo: async () => {
    if (readOnly()) return
    const pid = get().projectId
    if (!pid) return
    try {
      const { head } = await api.undo(pid)
      await get().reload()
      get().notify({ kind: 'info', text: `Undone — now at ${head}`, action: { label: 'Redo', run: () => void get().redo() } })
    } catch (e) { get().notify({ kind: 'error', text: errorText(e) }) }
  },
  redo: async () => {
    if (readOnly()) return
    const pid = get().projectId
    if (!pid) return
    try {
      const { head } = await api.redo(pid)
      await get().reload()
      get().notify({ kind: 'info', text: `Redone — now at ${head}` })
    } catch (e) { get().notify({ kind: 'error', text: errorText(e) }) }
  },

  sendMessage: async (text) => {
    const s = get()
    const next = continuation(threadRuns(s.runs, s.assistantCleared.runs), s.proposal, s.assistantNewRequest)
    const pid = s.projectId
    set({ assistantSending: text })
    try {
      if (next?.kind === 'proposal') return await get().replyToProposal(text, next.runId ?? undefined)
      return await get().assist(text, next?.runId ?? undefined)
    } finally {
      if (get().projectId === pid) set({ assistantSending: null })
    }
  },

  assist: async (instruction, parentRunId) => {
    if (readOnly()) return false
    const { projectId, model, selection, provider, runs, assistantCleared, proposal, selectionVersion, runSelectionVersion, assistantNoScope, assistantFresh } = get()
    if (!projectId || !model) return false
    const project = projectGeneration
    const request = ++assistantGeneration
    // Send what the composer shows as the scope; the server fills in the conversation's own scope
    // when it gets none (and not when the person dropped it).
    const thread = threadRuns(runs, assistantCleared.runs)
    const parent = parentRunId ? thread.find((r) => r.id === parentRunId) : undefined
    const next = parent ? continuation(thread, proposal, false) : null
    const picked = !!parent && selectionVersion > (runSelectionVersion[parent.id] ?? 0)
    const about = effectiveSelection(next, selection, thread, proposal, picked, assistantNoScope)
    const sent = about.source === 'current' ? about.selection
      : { ...emptySelection(), source_regions: selection.source_regions }
    // A new request replaces the proposal on screen; the server closes it so it does not come back later.
    const replaced = proposal && (proposal.status === 'pending' || proposal.status === 'stale') ? proposal.id : undefined
    try {
      const run = await api.assist(projectId, model.head, sent, instruction, provider ?? undefined, parentRunId,
        !(parent && assistantNoScope), replaced, !!parent && assistantFresh)
      set((s) => ({ runSelectionVersion: { ...s.runSelectionVersion, [run.id]: selectionVersion }, assistantFresh: false,
        ...(replaced ? { proposalStates: { ...s.proposalStates, [replaced]: 'dismissed' as const } } : {}) }))
      return acceptStartedRun(run, projectId, project, request, true)
    } catch (e) {
      if (project !== projectGeneration || get().projectId !== projectId) return false
      if (e instanceof ApiError && e.isStale) await get().reload()
      get().notify({ kind: 'error', text: `Could not start the assistant: ${errorText(e)}` })
      return false
    }
  },

  replyToProposal: async (instruction, parentRunId, retryLeftOut = false, given) => {
    if (readOnly()) return false
    const { projectId, provider, selection, assistantFresh } = get()
    const proposal = given ?? get().proposal
    if (!projectId || !proposal || proposal.status !== 'pending') return false
    const project = projectGeneration
    const request = ++assistantGeneration
    try {
      const run = await api.replyToProposal(projectId, proposal.id, instruction, provider ?? undefined, parentRunId,
        selection.source_regions, retryLeftOut, assistantFresh)
      set({ assistantFresh: false })
      return acceptStartedRun(run, projectId, project, request)
    } catch (e) {
      if (project !== projectGeneration || get().projectId !== projectId) return false
      if (e instanceof ApiError && e.isStale) await get().reload()
      get().notify({ kind: 'error', text: `Could not send reply: ${errorText(e)}` })
      return false
    }
  },

  startBuild: async (sourceIds, instruction, sourcePages, sourceRoles) => {
    if (readOnly()) return false
    const { projectId, model, provider, proposal } = get()
    if (!projectId || !model) return false
    const project = projectGeneration
    const request = ++assistantGeneration
    const replaced = proposal && (proposal.status === 'pending' || proposal.status === 'stale') ? proposal.id : undefined
    try {
      const run = await api.build(projectId, model.head, sourceIds, instruction, provider ?? undefined, sourcePages, sourceRoles, replaced)
      if (replaced) set((s) => ({ proposalStates: { ...s.proposalStates, [replaced]: 'dismissed' as const } }))
      return acceptStartedRun(run, projectId, project, request, true)
    } catch (e) {
      if (project !== projectGeneration || get().projectId !== projectId) return false
      if (e instanceof ApiError && e.isStale) await get().reload()
      get().notify({ kind: 'error', text: `Could not start the build: ${errorText(e)}` })
      return false
    }
  },

  cancelRun: async () => {
    const { projectId, activeRunId } = get()
    if (!projectId || !activeRunId) return
    const project = projectGeneration
    const run = await api.cancelRun(projectId, activeRunId)
    if (project !== projectGeneration || get().projectId !== projectId) return
    set({ runs: { ...get().runs, [run.id]: latestStartedRun(run, get().runs) } })
  },

  applyProposal: async (given) => {
    if (readOnly()) return
    const { projectId } = get()
    const proposal = given ?? get().proposal
    if (!projectId || !proposal) return
    const project = projectGeneration
    set({ busy: true })
    try {
      const rev = await api.applyProposal(projectId, proposal.id)
      if (project !== projectGeneration || get().projectId !== projectId) return
      // Applying one option of a choice settles it: the other options are closed.
      const siblings = siblingOptions(get().runs, proposal.id)
      await Promise.all(siblings.map((id) => api.dismissProposal(projectId, id).catch(() => {})))
      if (project !== projectGeneration || get().projectId !== projectId) return
      set({ ...(get().proposal?.id === proposal.id ? { proposal: { ...proposal, status: 'applied' as const, applied_revision: rev.id } } : {}),
            proposalStates: { ...get().proposalStates, [proposal.id]: 'applied',
                              ...Object.fromEntries(siblings.map((id) => [id, 'dismissed' as const])) } })
      await get().reload()
      if (project !== projectGeneration || get().projectId !== projectId) return
      void get().loadPending()
      if (get().proposal?.id === proposal.id) {
        void api.proposal(projectId, proposal.id).then((updated) => {
          if (project === projectGeneration && get().projectId === projectId && get().proposal?.id === proposal.id) {
            set({ proposal: updated })
          }
        }).catch(() => {})
      }
      const dismissed = proposal.issue_dismissals ?? []
      if (!proposal.operations.length && dismissed.length) {
        // Nothing changed in the model, so there is no revision to undo: offer to reopen instead.
        get().notify({ kind: 'success', text: `Dismissed ${dismissed.length} issue(s)`, action: { label: 'Reopen', run: () => {
          void Promise.all(dismissed.map((d) => api.setIssueState(projectId, d.id, 'open'))).then(() => get().reload())
        } } })
      } else {
        get().notify({ kind: 'success', text: `Applied as ${rev.id}${dismissed.length ? `; dismissed ${dismissed.length} issue(s)` : ''}`,
          action: { label: 'Undo', run: () => void get().undo() } })
      }
    } catch (e) {
      if (project !== projectGeneration || get().projectId !== projectId) return
      if (e instanceof ApiError && e.isStale) {
        if (get().proposal?.id === proposal.id) set({ proposal: { ...proposal, status: 'stale' } })
        await get().reload()
        void get().loadPending()
        get().notify({ kind: 'error', text: 'The model changed since this proposal was made. Regenerate it against the current model.' })
      } else get().notify({ kind: 'error', text: `Could not apply: ${errorText(e)}` })
    } finally {
      if (project === projectGeneration && get().projectId === projectId) set({ busy: false })
    }
  },

  dismissProposal: async (given) => {
    const { projectId } = get()
    const proposal = given ?? get().proposal
    if (!projectId || !proposal) return
    const project = projectGeneration
    try {
      await api.dismissProposal(projectId, proposal.id)
    } catch (e) {
      if (project === projectGeneration) get().notify({ kind: 'error', text: `Could not discard: ${errorText(e)}` })
      return
    }
    if (project !== projectGeneration || get().projectId !== projectId) return
    set({ ...(get().proposal?.id === proposal.id ? { proposal: null } : {}), proposalStates: { ...get().proposalStates, [proposal.id]: 'dismissed' },
          pendingEdits: get().pendingEdits.filter((p) => p.id !== proposal.id) })
  },

  regenerate: async (given) => {
    if (readOnly()) return
    const { projectId, provider } = get()
    const proposal = given ?? get().proposal
    if (!projectId || !proposal) return
    const project = projectGeneration
    const request = ++assistantGeneration
    try {
      const run = await api.regenerate(projectId, proposal.id, provider ?? undefined)
      acceptStartedRun(run, projectId, project, request)
    } catch (e) {
      if (project === projectGeneration && get().projectId === projectId) {
        get().notify({ kind: 'error', text: errorText(e) })
      }
    }
  },

  loadPending: async () => {
    const pid = get().projectId
    if (!pid) return
    const project = projectGeneration
    const generation = ++pendingGeneration
    try {
      const proposals = await api.proposals(pid)
      if (project !== projectGeneration || generation !== pendingGeneration || get().projectId !== pid) return
      set({ pendingEdits: openEdits(proposals), proposalStates: { ...get().proposalStates, ...proposalStates(proposals) } })
    } catch { /* the next event or reconnect refetches */ }
  },

  setModelVision: (choice, readsImages) => {
    if (get().modelVision[choice] !== readsImages) set({ modelVision: { ...get().modelVision, [choice]: readsImages } })
  },
  setProviderModels: (name, models) => {
    const known = get().providerModels[name]
    if (known && known.length === models.length && known.every((m, i) => m === models[i])) return
    set({ providerModels: { ...get().providerModels, [name]: models } })
  },
  setProvider: (provider) => {
    if (provider) localStorage.setItem('workbench.provider', provider)
    else localStorage.removeItem('workbench.provider')
    set({ provider })
  },

  handleEvent: (ev) => {
    const s = get()
    if (ev.type === 'revision' || ev.type === 'issues') {
      if (!s.model || ev.head !== s.model.head || ev.type === 'issues') void s.reload()
      if (ev.type === 'revision') {
        set({ sourcesVersion: get().sourcesVersion + 1 })  // record status follows the model
        void s.loadPending()  // waiting proposals were made against the earlier model
      }
    } else if (ev.type === 'sources' || ev.type === 'source_index') {
      set({ sourcesVersion: s.sourcesVersion + 1 })
    } else if (ev.type === 'run' && ev.run) {
      const run = ev.run
      set({ runs: { ...s.runs, [run.id]: run } })
      // A finished run may have proposed something (auto-fix adds its edits as it goes).
      if (run.status === 'succeeded' || run.progress?.at(-1)?.stage === 'group_done') void s.loadPending()
      const dismissed = run.status === 'succeeded' ? run.outcome.dismissed_proposal_id : null
      if (dismissed) {
        set({ proposalStates: { ...get().proposalStates, [dismissed]: 'dismissed' } })
        if (s.proposal?.id === dismissed) set({ proposal: null })
      }
      if (run.id === s.activeRunId && run.status === 'succeeded' && run.outcome.proposal_id
          && s.proposal?.id !== run.outcome.proposal_id && s.projectId) {
        const project = projectGeneration
        void api.proposal(s.projectId, run.outcome.proposal_id).then((proposal) => {
          if (project !== projectGeneration || get().activeRunId !== run.id) return
          set({
            proposal,
            proposalStates: { ...get().proposalStates, [proposal.id]: proposal.status,
              ...(proposal.parent_proposal_id ? { [proposal.parent_proposal_id]: 'superseded' as const } : {}) },
          })
        }).catch((e) => {
          if (project === projectGeneration) get().notify({ kind: 'error', text: `Could not load the proposed change: ${errorText(e)}` })
        })
      }
    }
  },

  notify: (toast) => set({ toast }),

  startAutofix: async (issues) => {
    if (readOnly()) return
    const { projectId, model, provider } = get()
    if (!projectId || !model || !issues.length) return
    const project = projectGeneration
    const request = ++autofixGeneration
    try {
      const run = await api.autofix(projectId, model.head, issues.map((i) => i.id), provider ?? undefined)
      if (project !== projectGeneration || get().projectId !== projectId) return
      set({ runs: { ...get().runs, [run.id]: latestStartedRun(run, get().runs) },
        ...(request === autofixGeneration ? { autofix: { runId: run.id } } : {}) })
    } catch (e) {
      if (project !== projectGeneration || get().projectId !== projectId) return
      if (e instanceof ApiError && e.isStale) await get().reload()
      get().notify({ kind: 'error', text: `Could not start auto-fix: ${errorText(e)}` })
    }
  },

  stopAutofix: async () => {
    const { projectId, autofix, runs } = get()
    const run = autofix ? runs[autofix.runId] : undefined
    if (projectId && run && isActive(run)) {
      const project = projectGeneration
      const cancelled = await api.cancelRun(projectId, run.id)
      if (project !== projectGeneration || get().projectId !== projectId) return
      set({ runs: { ...get().runs, [run.id]: latestStartedRun(cancelled, get().runs) } })
    } else {
      // Closing the report leaves its unanswered edits and choices waiting in the Pending edits tab.
      set({ autofix: null })
    }
  },

  undoAutofix: async () => {
    const { projectId, autofix, runs } = get()
    if (!projectId || !autofix || readOnly()) return
    const { revisions } = autofixReport(runs[autofix.runId])
    let undone = 0
    try {
      while (get().model && revisions.includes(get().model!.head)) {
        await api.undo(projectId)
        await get().reload()
        undone++
      }
    } catch (e) { get().notify({ kind: 'error', text: errorText(e) }) }
    get().notify(undone
      ? { kind: 'info', text: `Undid ${undone} automatic fix(es)`, action: { label: 'Redo', run: () => void get().redo() } }
      : { kind: 'info', text: 'Nothing to undo: the model has changed since the automatic fixes' })
  },

  showPending: (proposalId) => set((s) => ({ tab: 'pending', pendingFocus: proposalId ?? null,
    // the card a link points at unfolds (a choice's card is keyed by its first option)
    pendingCollapsed: proposalId ? s.pendingCollapsed.filter((id) => !sameCard(s, id, proposalId)) : s.pendingCollapsed })),
  togglePending: (cardId) => set((s) => ({ pendingCollapsed: s.pendingCollapsed.includes(cardId)
    ? s.pendingCollapsed.filter((id) => id !== cardId) : [...s.pendingCollapsed, cardId] })),

  viewRevision: async (viewing) => {
    if (viewing && get().autofix) set({ autofix: null })
    set({ viewing })
    await get().reload()
  },
}))

/** Whether two proposals share a Pending edits card (the same proposal, or options of one choice). */
function sameCard(s: State, a: string, b: string) {
  return a === b || siblingOptions(s.runs, a).includes(b)
}

/** True (with a notice) while an earlier revision is being browsed: nothing may change then. */
function readOnly() {
  const { viewing, notify } = useStore.getState()
  if (viewing) notify({ kind: 'info', text: `You are viewing ${viewing}, which is read-only. Go back to the current model to make changes.` })
  return !!viewing
}
