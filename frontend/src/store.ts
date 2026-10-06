import { create } from 'zustand'
import { api, ApiError } from './api'
import {
  autofixReport, continuation, isActive, issueSelection, issuesPrompt, proposalStates, threadRuns,
  type AutofixChoice, type AutofixState, type ProposalState,
} from './assistant'
import { applyClick, pruneSelection, type ClickTarget, type Modifiers } from './selection'
import {
  emptySelection, type AgentRun, type IssueRepair, type ViewInfo, type ModelResponse, type Proposal, type ReviewIssue, type Row, type Selection, type Status,
} from './types'

export type Tab = 'points' | 'equipment' | 'spaces' | 'connections' | 'connection_points' | 'graph' | 'issues' | `view:${string}`
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
  /** Repair-engine detail per issue id, for the revision named (loaded in the background). */
  repairs: { revision: string; byIssue: Record<string, IssueRepair> } | null
  assistantDraft: string
  assistantDraftVersion: number
  /** The next message starts a new request instead of continuing the conversation. */
  assistantNewRequest: boolean
  provider: string | null
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
  click: (target: ClickTarget, mods: Modifiers, order: string[]) => void
  setSelection: (sel: Selection) => void
  clearSelection: () => void
  setTab: (tab: Tab) => void
  setDrawerTab: (tab: DrawerTab) => void
  setAssistantDraft: (text: string) => void
  setAssistantNewRequest: (newRequest: boolean) => void
  focusAssistant: () => void
  appendAssistantContext: (text: string) => void
  /** Select what the issues affect and add them to the assistant prompt for review. */
  addIssuesToPrompt: (issues: ReviewIssue[]) => void
  inspect: (id: string | null) => void
  edit: (ops: Record<string, unknown>[], summary?: string) => Promise<boolean>
  undo: () => Promise<void>
  redo: () => Promise<void>
  sendMessage: (text: string) => Promise<boolean>
  assist: (instruction: string, parentRunId?: string) => Promise<boolean>
  replyToProposal: (instruction: string, parentRunId?: string) => Promise<boolean>
  startBuild: (sourceIds: string[], instruction: string, sourcePages?: Record<string, number[]>) => Promise<boolean>
  cancelRun: () => Promise<void>
  applyProposal: () => Promise<void>
  dismissProposal: () => Promise<void>
  regenerate: () => Promise<void>
  setProvider: (p: string) => void
  handleEvent: (ev: { type: string; head?: string; summary?: string; run?: AgentRun }) => void
  notify: (t: Toast | null) => void
  startAutofix: (issues: ReviewIssue[]) => Promise<void>
  /** Cancel a running auto-fix, or close the finished report. */
  stopAutofix: () => Promise<void>
  /** Undo the automatic revisions, newest first, while they are still the newest. */
  undoAutofix: () => Promise<void>
  /** Open a proposal auto-fix left for review in the assistant panel. */
  reviewProposal: (proposalId: string) => Promise<void>
  /** Pick an auto-fix option: apply its proposal (the person confirmed it) and discard the others. */
  chooseOption: (choice: AutofixChoice, proposalId: string) => Promise<void>
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
  repairs: null,
  assistantDraft: '',
  assistantDraftVersion: 0,
  assistantNewRequest: false,
  provider: null,
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
    set({ status, provider: get().provider ?? def?.name ?? null })
  },

  openProject: async (id) => {
    set({ projectId: id, model: null, rows: new Map(), selection: emptySelection(), proposal: null, proposalStates: {}, repairs: null,
          runs: {}, activeRunId: null, inspectId: null, drawerTab: 'inspector', assistantDraft: '', assistantNewRequest: false,
          autofix: null, viewing: null, views: [] })
    if (id) {
      localStorage.setItem('workbench.project', id)
      const stored = localStorage.getItem(`workbench.sourcesOpen.${id}`)
      set({ sourcesOpen: stored === 'true' })
      if (stored === null) void api.sources(id).then((list) => {
        if (get().projectId === id && localStorage.getItem(`workbench.sourcesOpen.${id}`) === null) {
          set({ sourcesOpen: list.length > 0 })
        }
      }).catch(() => {})
      await get().reload()
      void api.views(id).then((r) => { if (get().projectId === id) set({ views: r.views }) }).catch(() => {})
      // restore the conversation and the latest pending proposal, if any
      const [proposals, runs] = await Promise.all([api.proposals(id), api.runs(id).catch(() => [] as AgentRun[])])
      if (get().projectId !== id) return
      const merged = { ...Object.fromEntries(runs.map((r) => [r.id, r])), ...get().runs }
      const thread = threadRuns(merged)
      const autofixing = Object.values(merged).find((r) => r.kind === 'autofix' && isActive(r))
      set({ runs: merged, proposalStates: proposalStates(proposals),
            activeRunId: get().activeRunId ?? thread[thread.length - 1]?.id ?? null,
            autofix: get().autofix ?? (autofixing ? { runId: autofixing.id } : null) })
      const pending = proposals.find((p) => p.status === 'pending' || p.status === 'stale')
      if (pending) set({ proposal: pending })
    } else {
      localStorage.removeItem('workbench.project')
    }
  },

  reload: async () => {
    const pid = get().projectId
    if (!pid) return
    const viewing = get().viewing
    const model = await api.model(pid, viewing ?? undefined)
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
        if (get().projectId === pid && get().model?.revision.id === revision) set({ repairs: { revision, byIssue } })
      }).catch(() => {})
    }
  },

  click: (target, mods, order) => {
    const selection = applyClick(get().selection, target, mods, order, get().anchor)
    set({ selection, anchor: mods.shift ? get().anchor : target.id, inspectId: target.id })
  },
  setSelection: (selection) => set({ selection }),
  clearSelection: () => set({ selection: emptySelection(), anchor: null }),
  setTab: (tab) => set({ tab }),
  setDrawerTab: (drawerTab) => set({ drawerTab }),
  setAssistantDraft: (assistantDraft) => set({ assistantDraft }),
  setAssistantNewRequest: (assistantNewRequest) => set({ assistantNewRequest }),
  focusAssistant: () => set((s) => ({ assistantNewRequest: false, assistantDraftVersion: s.assistantDraftVersion + 1 })),
  appendAssistantContext: (context) => set((s) => ({
    assistantDraft: s.assistantDraft.trim() ? `${s.assistantDraft.trim()}\n\n${context}` : context,
    assistantDraftVersion: s.assistantDraftVersion + 1,
    assistantNewRequest: true,
  })),
  addIssuesToPrompt: (issues) => {
    if (!issues.length) return
    const { rows } = get()
    set({ selection: issueSelection(issues, rows) })
    get().appendAssistantContext(issuesPrompt(issues, rows))
  },
  inspect: (inspectId) => set({ inspectId }),

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
    const next = continuation(threadRuns(s.runs), s.proposal, s.assistantNewRequest)
    if (next?.kind === 'proposal') return get().replyToProposal(text, next.runId ?? undefined)
    return get().assist(text, next?.runId ?? undefined)
  },

  assist: async (instruction, parentRunId) => {
    if (readOnly()) return false
    const { projectId, model, selection, provider } = get()
    if (!projectId || !model) return false
    try {
      const run = await api.assist(projectId, model.head, selection, instruction, provider ?? undefined, parentRunId)
      set({ runs: { ...get().runs, [run.id]: run }, activeRunId: run.id, proposal: null, assistantNewRequest: false })
      return true
    } catch (e) {
      if (e instanceof ApiError && e.isStale) await get().reload()
      get().notify({ kind: 'error', text: `Could not start the assistant: ${errorText(e)}` })
      return false
    }
  },

  replyToProposal: async (instruction, parentRunId) => {
    if (readOnly()) return false
    const { projectId, proposal, provider } = get()
    if (!projectId || !proposal || proposal.status !== 'pending') return false
    try {
      const run = await api.replyToProposal(projectId, proposal.id, instruction, provider ?? undefined, parentRunId)
      set({ runs: { ...get().runs, [run.id]: run }, activeRunId: run.id, assistantNewRequest: false })
      return true
    } catch (e) {
      if (e instanceof ApiError && e.isStale) await get().reload()
      get().notify({ kind: 'error', text: `Could not send reply: ${errorText(e)}` })
      return false
    }
  },

  startBuild: async (sourceIds, instruction, sourcePages) => {
    if (readOnly()) return false
    const { projectId, model, provider } = get()
    if (!projectId || !model) return false
    try {
      const run = await api.build(projectId, model.head, sourceIds, instruction, provider ?? undefined, sourcePages)
      set({ runs: { ...get().runs, [run.id]: run }, activeRunId: run.id, proposal: null })
      return true
    } catch (e) {
      if (e instanceof ApiError && e.isStale) await get().reload()
      get().notify({ kind: 'error', text: `Could not start the build: ${errorText(e)}` })
      return false
    }
  },

  cancelRun: async () => {
    const { projectId, activeRunId } = get()
    if (!projectId || !activeRunId) return
    const run = await api.cancelRun(projectId, activeRunId)
    set({ runs: { ...get().runs, [run.id]: run } })
  },

  applyProposal: async () => {
    if (readOnly()) return
    const { projectId, proposal } = get()
    if (!projectId || !proposal) return
    set({ busy: true })
    try {
      const rev = await api.applyProposal(projectId, proposal.id)
      set({ proposal: { ...proposal, status: 'applied', applied_revision: rev.id },
            proposalStates: { ...get().proposalStates, [proposal.id]: 'applied' } })
      await get().reload()
      void api.proposal(projectId, proposal.id).then((updated) => set({ proposal: updated })).catch(() => {})
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
      if (e instanceof ApiError && e.isStale) {
        set({ proposal: { ...proposal, status: 'stale' } })
        await get().reload()
        get().notify({ kind: 'error', text: 'The model changed since this proposal was made. Regenerate it against the current model.' })
      } else get().notify({ kind: 'error', text: `Could not apply: ${errorText(e)}` })
    } finally {
      set({ busy: false })
    }
  },

  dismissProposal: async () => {
    const { projectId, proposal } = get()
    if (!projectId || !proposal) return
    await api.dismissProposal(projectId, proposal.id)
    set({ proposal: null, proposalStates: { ...get().proposalStates, [proposal.id]: 'dismissed' } })
  },

  regenerate: async () => {
    if (readOnly()) return
    const { projectId, proposal, provider } = get()
    if (!projectId || !proposal) return
    try {
      const run = await api.regenerate(projectId, proposal.id, provider ?? undefined)
      set({ runs: { ...get().runs, [run.id]: run }, activeRunId: run.id })
    } catch (e) { get().notify({ kind: 'error', text: errorText(e) }) }
  },

  setProvider: (provider) => set({ provider }),

  handleEvent: (ev) => {
    const s = get()
    if (ev.type === 'revision' || ev.type === 'issues') {
      if (!s.model || ev.head !== s.model.head || ev.type === 'issues') void s.reload()
      if (ev.type === 'revision') set({ sourcesVersion: get().sourcesVersion + 1 })  // record status follows the model
    } else if (ev.type === 'sources') {
      set({ sourcesVersion: s.sourcesVersion + 1 })
    } else if (ev.type === 'run' && ev.run) {
      const run = ev.run
      set({ runs: { ...s.runs, [run.id]: run } })
      const dismissed = run.status === 'succeeded' ? run.outcome.dismissed_proposal_id : null
      if (dismissed) {
        set({ proposalStates: { ...get().proposalStates, [dismissed]: 'dismissed' } })
        if (s.proposal?.id === dismissed) set({ proposal: null })
      }
      if (run.id === s.activeRunId && run.status === 'succeeded' && run.outcome.proposal_id
          && s.proposal?.id !== run.outcome.proposal_id && s.projectId) {
        void api.proposal(s.projectId, run.outcome.proposal_id).then((proposal) => set({
          proposal,
          proposalStates: { ...get().proposalStates, [proposal.id]: proposal.status,
            ...(proposal.parent_proposal_id ? { [proposal.parent_proposal_id]: 'superseded' as const } : {}) },
        }))
      }
    }
  },

  notify: (toast) => set({ toast }),

  startAutofix: async (issues) => {
    if (readOnly()) return
    const { projectId, model, provider } = get()
    if (!projectId || !model || !issues.length) return
    try {
      const run = await api.autofix(projectId, model.head, issues.map((i) => i.id), provider ?? undefined)
      set({ runs: { ...get().runs, [run.id]: run }, autofix: { runId: run.id } })
    } catch (e) {
      if (e instanceof ApiError && e.isStale) await get().reload()
      get().notify({ kind: 'error', text: `Could not start auto-fix: ${errorText(e)}` })
    }
  },

  stopAutofix: async () => {
    const { projectId, autofix, runs } = get()
    const run = autofix ? runs[autofix.runId] : undefined
    if (projectId && run && isActive(run)) {
      const cancelled = await api.cancelRun(projectId, run.id)
      set({ runs: { ...get().runs, [run.id]: cancelled } })
    } else set({ autofix: null })
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

  reviewProposal: async (proposalId) => {
    const { projectId } = get()
    if (!projectId) return
    const proposal = await api.proposal(projectId, proposalId)
    set({ proposal, proposalStates: { ...get().proposalStates, [proposal.id]: proposal.status } })
  },

  chooseOption: async (choice, proposalId) => {
    const { projectId } = get()
    if (!projectId || readOnly()) return
    set({ busy: true })
    try {
      const rev = await api.applyProposal(projectId, proposalId)
      const others = choice.options.map((o) => o.proposal_id).filter((id) => id !== proposalId)
      await Promise.all(others.map((id) => api.dismissProposal(projectId, id).catch(() => {})))
      set({ proposalStates: { ...get().proposalStates, [proposalId]: 'applied',
                              ...Object.fromEntries(others.map((id) => [id, 'dismissed' as const])) } })
      await get().reload()
      const label = choice.options.find((o) => o.proposal_id === proposalId)?.label ?? 'the option'
      get().notify({ kind: 'success', text: `Applied ${label} as ${rev.id}`, action: { label: 'Undo', run: () => void get().undo() } })
    } catch (e) {
      get().notify({ kind: 'error', text: `Could not apply: ${errorText(e)}` })
    } finally {
      set({ busy: false })
    }
  },

  viewRevision: async (viewing) => {
    if (viewing && get().autofix) set({ autofix: null })
    set({ viewing })
    await get().reload()
  },
}))

/** True (with a notice) while an earlier revision is being browsed: nothing may change then. */
function readOnly() {
  const { viewing, notify } = useStore.getState()
  if (viewing) notify({ kind: 'info', text: `You are viewing ${viewing}, which is read-only. Go back to the current model to make changes.` })
  return !!viewing
}
