import { emptySelection, type AgentRun, type EntityChange, type ProgressEvent, type Proposal, type ReviewIssue, type Row, type Selection, type TokenUsage } from './types'

export type ProposalState = Proposal['status'] | 'superseded'

/** What the next message continues: the pending proposal, the assistant's questions, or the conversation. */
export type Continuation = { kind: 'proposal' | 'questions' | 'conversation'; runId: string | null }

export const isActive = (run: AgentRun) => run.status === 'queued' || run.status === 'running'

/** Merge persisted history with live run updates without counting a run twice. */
export function tokenTotals(recorded: Record<string, TokenUsage>, runs: Record<string, AgentRun>): TokenUsage {
  const usage = { ...recorded }
  for (const run of Object.values(runs)) {
    usage[run.id] = {
      input_tokens: Math.max(usage[run.id]?.input_tokens ?? 0, run.outcome.input_tokens ?? 0),
      output_tokens: Math.max(usage[run.id]?.output_tokens ?? 0, run.outcome.output_tokens ?? 0),
    }
  }
  return Object.values(usage).reduce((total, run) => ({
    input_tokens: total.input_tokens + run.input_tokens,
    output_tokens: total.output_tokens + run.output_tokens,
  }), { input_tokens: 0, output_tokens: 0 })
}

/** Assistant runs in the order they were requested. */
export function threadRuns(runs: Record<string, AgentRun>, hiddenIds: string[] = []): AgentRun[] {
  // Auto-fix runs report in their own banner, not as conversation turns.
  const hidden = new Set(hiddenIds)
  return Object.values(runs).filter((r) => r.kind !== 'autofix' && !hidden.has(r.id))
    .sort((a, b) => a.created_at.localeCompare(b.created_at))
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
  issues = [...new Map(issues.map((issue) => [issue.id, issue])).values()]
  if (!issues.length) return ''
  type Entry = { issue: ReviewIssue; message: string; metadata: Record<string, unknown>; specific: string[] }
  const entries: Entry[] = []
  for (const issue of issues) {
    const base = { Severity: issue.severity, Category: issue.category, Origin: issue.origin, State: issue.resolution_state }
    const { findings, ...details } = issue.details
    const detail = Object.keys(details).length ? [`Details: ${JSON.stringify(details)}`] : []
    if (!findings?.length) {
      entries.push({ issue, message: issue.explanation, metadata: base, specific: detail })
      continue
    }
    for (const finding of findings) {
      const metadata: Record<string, unknown> = { ...base }
      const specific = [...detail]
      if (finding.path) metadata.Path = finding.path
      if (finding.shape) metadata.Shape = finding.shape
      if (finding.severity) metadata['Finding severity'] = finding.severity
      if (finding.statement_id != null) metadata.Statement = finding.statement_id
      const primary = rows.get(issue.affected_ids[0])
      if (primary?.iri && finding.focus === primary.iri) metadata.Focus = "first affected object's IRI"
      else if (finding.focus) specific.push(`Focus: ${finding.focus}`)
      if (finding.value != null) {
        if (finding.value === finding.focus) metadata.Value = 'same as focus'
        else specific.push(`Value: ${JSON.stringify(finding.value)}`)
      }
      for (const [key, value] of Object.entries(finding)) {
        if (!['focus', 'value', 'message', 'path', 'shape', 'severity', 'statement_id'].includes(key)) metadata[`Finding ${key}`] = value
      }
      // Validation explanations are only rendered copies of the authoritative finding.
      if (issue.origin !== 'validation') metadata.Explanation = issue.explanation
      entries.push({ issue, message: finding.message, metadata, specific })
    }
  }
  const equal = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)
  const shared = Object.fromEntries(Object.entries(entries[0].metadata)
    .filter(([key, value]) => entries.every((entry) => key in entry.metadata && equal(entry.metadata[key], value))))
  const format = (metadata: Record<string, unknown>) => Object.entries(metadata)
    .map(([key, value]) => `${key}: ${typeof value === 'string' ? value : JSON.stringify(value)}`).join('; ')
  const groups = new Map<string, { metadata: Record<string, unknown>; messages: Map<string, Entry[]> }>()
  for (const entry of entries) {
    const metadata = Object.fromEntries(Object.entries(entry.metadata).filter(([key]) => !(key in shared))
      .sort(([a], [b]) => a.localeCompare(b)))
    const key = JSON.stringify(metadata)
    if (!groups.has(key)) groups.set(key, { metadata, messages: new Map() })
    const messages = groups.get(key)!.messages
    if (!messages.has(entry.message)) messages.set(entry.message, [])
    messages.get(entry.message)!.push(entry)
  }
  const objects = [...new Set(issues.flatMap((issue) => issue.affected_ids))].map((id) => {
    const row = rows.get(id)
    return row ? `${row.label} [${id}] (${row.kind}${row.iri ? `; IRI: ${row.iri}` : ''})` : `[${id}]`
  })
  const sections = [...groups.values()].map((group) => [
    format(group.metadata),
    ...[...group.messages].map(([message, members]) => [
      `- ${message}`,
      ...members.map(({ issue, specific }) => `  [${issue.id}] Affected: ${[...new Set(issue.affected_ids)].join(', ') || 'none'}${specific.length ? `; ${specific.join('; ')}` : ''}`),
    ].join('\n')),
  ].filter(Boolean).join('\n'))
  return [
    `Please review ${issues.length === 1 ? 'this issue' : `these ${issues.length} issues`} against the current model. Fix related issues together when appropriate; explain any that need separate handling or no longer apply.`,
    objects.length ? `Objects: ${objects.join('; ')}` : '',
    format(shared),
    ...sections,
  ].filter(Boolean).join('\n\n')
}

const TOOL_STEPS: Record<string, (arg: string) => string> = {
  search_terms: (a) => `Looked up vocabulary terms for ${a}`,
  units_for: (a) => `Looked up units for ${a}`,
  describe_class: (a) => `Read the definition of ${a}`,
  find_entities: (a) => `Searched the model for ${a}`,
  relations_for: (a) => `Looked up the relations allowed for ${a}`,
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
  const key = ['query', 'term', 'quantity_kind', 'topic', 'observation_id', 'entity_id'].find((k) => args.get(k))
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

/** How auto-fix left a group of issues (backend autofix.py): fixed automatically after its checks
 * passed, options to choose from, a proposal to review, the assistant's questions, a failure, or
 * already fixed. */
export type AutofixStatus = 'fixed' | 'choice' | 'review' | 'input' | 'failed' | 'resolved'
/** An option is a checked pending proposal: choosing it applies it. */
export interface AutofixOption { label: string; proposal_id: string; note?: string }
export interface AutofixChoice { question: string; options: AutofixOption[] }
export interface AutofixGroup {
  issues: string[]; explanations: string[]; status: AutofixStatus; reasons?: string[]
  proposal_id?: string; revision?: string; explanation?: string; questions?: string[]
  choices?: AutofixChoice[]
}
export interface AutofixState { runId: string }

/** The groups an auto-fix run has finished so far, and the automatic revisions it made. */
export function autofixReport(run: AgentRun | undefined): { groups: AutofixGroup[]; revisions: string[] } {
  const done = run?.outcome?.autofix as { groups: AutofixGroup[]; revisions: string[] } | undefined
  if (done) return done
  const groups = (run?.progress ?? []).filter((p) => p.stage === 'group_done')
    .map((p) => ({ issues: [], explanations: [], status: p.data.status as AutofixStatus, reasons: p.data.reasons as string[] }))
  return { groups, revisions: [] }
}

/** Where a running auto-fix is: group k of n, and its latest step. */
export function autofixProgress(run: AgentRun | undefined): { group: number; total: number; message: string } {
  const events = run?.progress ?? []
  const total = Number(events.find((p) => p.stage === 'autofix')?.data.groups ?? 0)
  const group = Number([...events].reverse().find((p) => p.stage === 'group')?.data.group ?? 0)
  return { group, total, message: events.at(-1)?.message ?? 'Starting…' }
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

/** Open issues (not suggestions) on the selected objects, in auto-fix order. */
export function issuesOnSelection(issues: ReviewIssue[], sel: Selection): ReviewIssue[] {
  const chosen = new Set([...sel.entity_ids, ...sel.relationship_ids])
  const ids = issues.filter((i) => i.resolution_state === 'open' && i.severity !== 'suggestion'
    && i.affected_ids.some((a) => chosen.has(a))).map((i) => i.id)
  return ids.length ? autofixCandidates(issues, new Set(ids)) : []  // (no ticks would mean "every violation")
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
    const fields = c.change === 'created' ? c.fields.filter((f) => f.field !== 'label') : c.fields
    if (c.change !== 'deleted' && fields.length) {
      const value = (v: unknown) => v === null || v === undefined || v === '' ? 'none' : String(v)
      const shown = fields.slice(0, 2).map((f) => c.change === 'updated'
        ? `${fieldLabel(f.field)}: ${value(f.before)} → ${value(f.after)}`
        : `${fieldLabel(f.field)}: ${value(f.after)}`)
      detail = (c.change === 'created' ? `${kind} · ` : '') + shown.join('; ')
        + (fields.length > 2 ? ` (+${fields.length - 2} more)` : '')
    }
    return { id: c.entity_id, mark: MARKS[c.change], change: c.change, label: c.label, kind, detail,
             outside: !c.in_selection, overridesEdit: c.overrides_locked.length > 0 }
  })
}
