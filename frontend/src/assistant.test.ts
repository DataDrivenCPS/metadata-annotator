import { describe, expect, it } from 'vitest'
import {
  autofixCandidates, continuation, describeStep, effectiveSelection, formatDuration, issueSelection, issuesOnSelection, nextAutofixIssue,
  proposalStates,
  startsExchange, summarizeChanges, threadRuns,
} from './assistant'
import { emptySelection, type AgentRun, type Proposal, type ReviewIssue, type Row } from './types'

const run = (id: string, created_at: string, extra: Partial<AgentRun> = {}): AgentRun => ({
  id, kind: 'correction', input_revision: 'rev-1', selection: null, instruction: id, provider: 'p', model: 'm',
  skill_version: '', status: 'succeeded', progress: [], outcome: {}, error: null, created_at, finished_at: created_at,
  ...extra,
})

describe('assistant thread', () => {
  it('orders runs by request time', () => {
    const t = threadRuns({ b: run('b', '2026-01-02'), a: run('a', '2026-01-01') })
    expect(t.map((r) => r.id)).toEqual(['a', 'b'])
  })

  it('answers questions from the last run', () => {
    const asked = run('a', '1', { outcome: { questions: ['Which tank?'], proposal_id: null } })
    expect(continuation([asked], null, false)).toEqual({ kind: 'questions', runId: 'a' })
  })

  it('replies to a pending proposal, continuing from the latest finished run', () => {
    const pending = { id: 'p1', status: 'pending' } as Proposal
    const t = [run('a', '1', { outcome: { proposal_id: 'p1' } })]
    expect(continuation(t, pending, false)).toEqual({ kind: 'proposal', runId: 'a' })
    expect(continuation([...t, run('b', '2', { status: 'running' })], pending, false))
      .toEqual({ kind: 'proposal', runId: null })
  })

  it('starts fresh when asked to or when there is nothing to continue', () => {
    expect(continuation([run('a', '1')], null, true)).toBeNull()
    expect(continuation([], null, false)).toBeNull()
    expect(continuation([run('a', '1')], null, false)).toEqual({ kind: 'conversation', runId: 'a' })
  })

  it('marks new exchanges and revised proposals', () => {
    expect(startsExchange(run('a', '1'))).toBe(true)
    expect(startsExchange(run('b', '2', { parent_run_id: 'a' }))).toBe(false)
    expect(startsExchange(run('c', '3', { mode: 'reply' }))).toBe(false)
    expect(proposalStates([
      { id: 'p1', status: 'dismissed' } as Proposal,
      { id: 'p2', status: 'pending', parent_proposal_id: 'p1' } as Proposal,
    ])).toEqual({ p1: 'superseded', p2: 'pending' })
  })
})

describe('effective selection', () => {
  const about = (...ids: string[]) => ({ ...emptySelection(), entity_ids: ids })
  const asked = run('a', '1', { selection: about('eq-ro1'), outcome: { questions: ['Expected?'] } })

  it('keeps the conversation selection when nothing is selected now', () => {
    const next = continuation([asked], null, false)
    expect(effectiveSelection(next, emptySelection(), [asked], null))
      .toEqual({ selection: about('eq-ro1'), source: 'conversation' })
  })

  it('uses a new selection, or the proposal selection for replies', () => {
    const next = continuation([asked], null, false)
    expect(effectiveSelection(next, about('eq-p201'), [asked], null).source).toBe('current')
    expect(effectiveSelection(null, emptySelection(), [asked], null).source).toBe('current')
    const proposal = { id: 'p', status: 'pending', selection: about('eq-tk') } as Proposal
    expect(effectiveSelection(continuation([asked], proposal, false), about('eq-p201'), [asked], proposal))
      .toEqual({ selection: about('eq-tk'), source: 'proposal' })
  })
})

describe('run steps', () => {
  const step = (stage: string, message: string, data: Record<string, unknown> = {}) => ({ at: '', stage, message, data })

  it('describes tool calls in plain words', () => {
    expect(describeStep(step('tool', "search_terms(query='pressure', kind='quantity_kind')")))
      .toBe('Looked up vocabulary terms for “pressure” (quantity kind)')
    expect(describeStep(step('tool', "search_terms(kind='equipment', query='Air Handling Unit')")))
      .toBe('Looked up vocabulary terms for “Air Handling Unit” (equipment)')
    expect(describeStep(step('tool', "describe_class(term='watr:Pump')"))).toBe('Read the definition of “watr:Pump”')
    expect(describeStep(step('tool', 'find_entities(query="it\'s RO-1")'))).toBe('Searched the model for “it\'s RO-1”')
    expect(describeStep(step('tool', 'mystery_tool(x=1)'))).toBe('mystery_tool(x=1)')
    expect(describeStep(step('context', 'Read the selection'))).toBe('Read the selection')
  })

  it('numbers repeated model calls', () => {
    expect(describeStep(step('model', 'Asking morgan (Qwen)', { step: 1 }))).toBe('Asking morgan (Qwen)')
    expect(describeStep(step('model', 'Asking morgan (Qwen)', { step: 3 }))).toBe('Asking morgan (Qwen) again (step 3)')
  })

  it('formats durations', () => {
    expect(formatDuration(0)).toBe('0:00')
    expect(formatDuration(65_400)).toBe('1:05')
    expect(formatDuration(-500)).toBe('0:00')
  })

  it('selects what issues affect, splitting connections from entities', () => {
    const rows = new Map([['eq-1', { id: 'eq-1', kind: 'equipment' }], ['cx-1', { id: 'cx-1', kind: 'connection' }]]) as unknown as Map<string, Row>
    const issue = (affected_ids: string[]) => ({ affected_ids }) as ReviewIssue
    const sel = issueSelection([issue(['eq-1', 'cx-1']), issue(['eq-1', 'gone'])], rows)
    expect(sel.entity_ids).toEqual(['eq-1'])
    expect(sel.relationship_ids).toEqual(['cx-1'])
  })
})

describe('auto-fix helpers', () => {
  const issue = (id: string, eq: string, severity: ReviewIssue['severity'] = 'violation', state: ReviewIssue['resolution_state'] = 'open') =>
    ({ id, affected_ids: [eq], severity, resolution_state: state, explanation: id }) as ReviewIssue
  const issues = [issue('a', 'eq-1'), issue('w', 'eq-2', 'warning'), issue('b', 'eq-2'), issue('c', 'eq-1'),
    issue('d', 'eq-3', 'violation', 'dismissed')]

  it('takes open violations by default, keeping one object together', () => {
    expect(autofixCandidates(issues, new Set()).map((i) => i.id)).toEqual(['a', 'c', 'b'])
  })

  it('takes exactly the ticked open issues when some are ticked', () => {
    expect(autofixCandidates(issues, new Set(['w', 'd'])).map((i) => i.id)).toEqual(['w'])
  })

  it('finds the open issues on the selected objects', () => {
    const sel = { ...emptySelection(), entity_ids: ['eq-2', 'eq-3'] }
    expect(issuesOnSelection(issues, sel).map((i) => i.id)).toEqual(['w', 'b'])
  })

  it('skips queued issues that are no longer open', () => {
    const queue = ['a', 'x', 'b'].map((id) => ({ id, explanation: id }))
    const { next, rest, resolved } = nextAutofixIssue(queue.slice(1), issues)
    expect(next?.id).toBe('b')
    expect(resolved.map((r) => r.id)).toEqual(['x'])
    expect(rest).toEqual([])
    expect(nextAutofixIssue([{ id: 'x', explanation: 'x' }], issues).next).toBeNull()
  })
})

describe('proposal change lines', () => {
  const change = (id: string, kind: 'created' | 'updated' | 'deleted', fields: [string, unknown][] = []) => ({
    entity_id: id, entity_kind: 'connection_point', label: id, change: kind, in_selection: id !== 'out',
    overrides_locked: [], fields: fields.map(([field, after]) => ({ field, before: null, after })),
  })

  it('lists additions first and summarizes updated fields', () => {
    const lines = summarizeChanges([
      change('upd', 'updated', [['maps_to', 'AHU inlet'], ['paired_with', null], ['medium', 'Water']]),
      change('gone', 'deleted'), change('out', 'created'),
    ], (f) => f.replace('_', ' '))
    expect(lines.map((l) => [l.mark, l.id])).toEqual([['+', 'out'], ['~', 'upd'], ['−', 'gone']])
    expect(lines[0]).toMatchObject({ detail: 'connection point', outside: true })
    expect(lines[1].detail).toBe('maps to → AHU inlet; paired with → none (+1 more)')
  })
})
