import { emptySelection, type AgentRun, type EntityChange, type ProgressEvent, type Proposal, type ReviewIssue, type Row, type Selection } from './types'

export type ProposalState = Proposal['status'] | 'superseded'

/** What the next message continues: the pending proposal, the assistant's questions, or the conversation. */
export type Continuation = { kind: 'proposal' | 'questions' | 'conversation'; runId: string | null }

export const isActive = (run: AgentRun) => run.status === 'queued' || run.status === 'running'

/** Assistant runs in the order they were requested. */
export function threadRuns(runs: Record<string, AgentRun>): AgentRun[] {
  return Object.values(runs).sort((a, b) => a.created_at.localeCompare(b.created_at))
}

/** A run that does not continue an earlier one starts a new exchange in the thread. */
export const startsExchange = (run: AgentRun) =>
  run.mode === 'build' || ((run.mode ?? 'assist') === 'assist' && !run.parent_run_id)

export function continuation(thread: AgentRun[], proposal: Proposal | null, newRequest: boolean): Continuation | null {
  if (newRequest) return null
  const last = thread[thread.length - 1]
  const runId = last && !isActive(last) ? last.id : null
  if (proposal?.status === 'pending') return { kind: 'proposal', runId }
  if (!last || !runId) return null
  if (last.status === 'succeeded' && !last.outcome.proposal_id && last.outcome.questions?.length) {
    return { kind: 'questions', runId }
  }
  return { kind: 'conversation', runId }
}

/** Proposal states for the thread: a proposal revised by a later reply is superseded. */
export function proposalStates(proposals: Proposal[]): Record<string, ProposalState> {
  const out: Record<string, ProposalState> = {}
  for (const p of proposals) out[p.id] = p.status
  for (const p of proposals) if (p.parent_proposal_id && out[p.parent_proposal_id]) out[p.parent_proposal_id] = 'superseded'
  return out
}

export type SelectionSource = 'current' | 'conversation' | 'proposal'

const isEmpty = (s: Selection) =>
  !s.entity_ids.length && !s.relationship_ids.length && !s.field_ids.length && !s.source_regions.length

/** The selection the next message will be about, matching what the server uses. */
export function effectiveSelection(next: Continuation | null, current: Selection, thread: AgentRun[],
                                   proposal: Proposal | null): { selection: Selection; source: SelectionSource } {
  if (next?.kind === 'proposal' && proposal) return { selection: proposal.selection, source: 'proposal' }
  const parent = next?.runId ? thread.find((r) => r.id === next.runId) : null
  if (parent?.selection && isEmpty(current) && !isEmpty(parent.selection)) {
    return { selection: parent.selection, source: 'conversation' }
  }
  return { selection: current, source: 'current' }
}

/** Select the objects these issues affect, as the assistant prompt about them refers to. */
export function issueSelection(issues: ReviewIssue[], rows: Map<string, Row>): Selection {
  const ids = [...new Set(issues.flatMap((issue) => issue.affected_ids))].filter((id) => rows.has(id))
  const rel = ids.filter((id) => rows.get(id)!.kind === 'connection')
  return { ...emptySelection(), entity_ids: ids.filter((id) => !rel.includes(id)), relationship_ids: rel }
}

export function issuesPrompt(issues: ReviewIssue[], rows: Map<string, Row>): string {
  const descriptions = issues.map((issue, index) => {
    const objects = issue.affected_ids.map((id) => {
      const row = rows.get(id)
      return row ? `${row.label} (${row.kind}, ${id})` : id
    })
    const findings = issue.details.findings?.map((finding) => [
      finding.message,
      finding.focus ? `focus ${finding.focus}` : '',
      finding.path ? `path ${finding.path}` : '',
      finding.shape ? `shape ${finding.shape}` : '',
    ].filter(Boolean).join('; ')) ?? []
    return [
      `${issues.length > 1 ? `${index + 1}. ` : ''}[${issue.id}] ${issue.severity}, ${issue.category.replaceAll('_', ' ')}: ${issue.explanation}`,
      objects.length ? `Affected objects: ${objects.join('; ')}` : '',
      findings.length ? `Validation details: ${findings.join(' | ')}` : '',
    ].filter(Boolean).join('\n')
  })
  if (issues.length === 1) return [
    'Please review this issue. Check it against the current model and fix it, or explain why it no longer applies.',
    descriptions[0],
  ].join('\n\n')
  return [
    'Please review these selected issues together. Check each against the current model, fix them together when they share an underlying cause, and explain any that should be handled separately or no longer apply.',
    `Selected issues (${issues.length}):`,
    descriptions.join('\n\n'),
  ].join('\n\n')
}

const TOOL_STEPS: Record<string, (arg: string) => string> = {
  search_terms: (a) => `Looked up vocabulary terms for ${a}`,
  units_for: (a) => `Looked up units for ${a}`,
  describe_class: (a) => `Read the definition of ${a}`,
  find_entities: (a) => `Searched the model for ${a}`,
  read_evidence: (a) => `Read source evidence ${a}`,
  read_guidance: (a) => `Read the modeling guidance on ${a}`,
}

/** A progress event in plain words: tool calls arrive as ``name(arg='value', ...)``. */
export function describeStep(event: ProgressEvent): string {
  if (event.stage === 'model') {
    const step = Number(event.data.step ?? 1)
    return step > 1 ? `${event.message} again (step ${step})` : event.message
  }
  const call = event.stage === 'tool' ? /^(\w+)\((.*)\)$/s.exec(event.message) : null
  const describe = call && TOOL_STEPS[call[1]]
  if (!call || !describe) return event.message
  const args = new Map<string, string>()
  for (const m of call[2].matchAll(/(\w+)=(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)"|([^,]*))/g)) {
    args.set(m[1], (m[2] ?? m[3] ?? m[4]).trim())
  }
  // Name the step after what was looked up, not a filter such as kind='equipment'.
  const key = ['query', 'term', 'quantity_kind', 'topic', 'observation_id'].find((k) => args.get(k))
  const arg = key ? args.get(key)! : (args.values().next().value ?? '')
  const kind = args.get('kind') && key === 'query' ? ` (${args.get('kind')!.replaceAll('_', ' ')})` : ''
  return describe(arg ? `“${arg}”${kind}` : 'a term').replace(/ “”$/, '')
}

/** Elapsed time as m:ss. */
export function formatDuration(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

// ------------------------------------------------------------------ auto-fix

export type AutofixOutcome = 'applied' | 'dismissed' | 'skipped' | 'resolved' | 'failed'
export interface AutofixItem { id: string; explanation: string }
export interface AutofixState {
  queue: AutofixItem[]
  total: number
  /** The issue being worked on and the runs of its conversation (the first request and any replies). */
  current: (AutofixItem & { runIds: string[] }) | null
  results: (AutofixItem & { outcome: AutofixOutcome })[]
  /** Why the loop is waiting for the person; null while the assistant works. */
  paused: 'review' | 'input' | 'failed' | null
}

/** The issues ticked in the list, else the open violations; one object's issues stay together. */
export function autofixCandidates(issues: ReviewIssue[], ticked: Set<string>): ReviewIssue[] {
  const open = issues.filter((i) => i.resolution_state === 'open')
  const chosen = ticked.size ? open.filter((i) => ticked.has(i.id)) : open.filter((i) => i.severity === 'violation')
  const owner = (i: ReviewIssue) => i.affected_ids[0] ?? i.id
  const rank = new Map<string, number>()
  for (const i of chosen) if (!rank.has(owner(i))) rank.set(owner(i), rank.size)
  return chosen.map((issue, index) => ({ issue, index }))
    .sort((a, b) => rank.get(owner(a.issue))! - rank.get(owner(b.issue))! || a.index - b.index)
    .map((x) => x.issue)
}

/** The next queued issue still open; the ones skipped over were resolved by earlier fixes. */
export function nextAutofixIssue(queue: AutofixItem[], issues: ReviewIssue[]):
    { next: ReviewIssue | null; rest: AutofixItem[]; resolved: AutofixItem[] } {
  const open = new Map(issues.filter((i) => i.resolution_state === 'open').map((i) => [i.id, i]))
  for (let k = 0; k < queue.length; k++) {
    const issue = open.get(queue[k].id)
    if (issue) return { next: issue, rest: queue.slice(k + 1), resolved: queue.slice(0, k) }
  }
  return { next: null, rest: [], resolved: queue }
}

/** What finished the run: a proposal to review, a reply that needs the person, or a failure. */
export function autofixPause(run: AgentRun): AutofixState['paused'] {
  if (run.status === 'failed' || run.status === 'cancelled') return 'failed'
  return run.outcome.proposal_id ? 'review' : 'input'
}

// ------------------------------------------------------------- proposal card

export interface ChangeLine {
  id: string; mark: '+' | '~' | '−'; change: EntityChange['change']; label: string
  kind: string; detail: string; outside: boolean; overridesEdit: boolean
}

const MARKS = { created: '+', updated: '~', deleted: '−' } as const

/** One line per changed object, added first, then changed, then removed. */
export function summarizeChanges(changes: EntityChange[], fieldLabel: (field: string) => string): ChangeLine[] {
  const order = { created: 0, updated: 1, deleted: 2 }
  return [...changes].sort((a, b) => order[a.change] - order[b.change]).map((c) => {
    const kind = c.entity_kind.replace(/_/g, ' ')
    let detail = kind
    if (c.change === 'updated') {
      const shown = c.fields.slice(0, 2).map((f) => `${fieldLabel(f.field)} → ${f.after === null || f.after === undefined || f.after === '' ? 'none' : String(f.after)}`)
      detail = shown.join('; ') + (c.fields.length > 2 ? ` (+${c.fields.length - 2} more)` : '')
    }
    return { id: c.entity_id, mark: MARKS[c.change], change: c.change, label: c.label, kind, detail,
             outside: !c.in_selection, overridesEdit: c.overrides_locked.length > 0 }
  })
}
