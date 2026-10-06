import { describe, expect, it } from 'vitest'
import {
  autofixCandidates, autofixProgress, autofixReport, continuation, describeStep, effectiveSelection, formatDuration, issueSelection, issuesOnSelection, issuesPrompt,
  proposalStates,
  startsExchange, summarizeChanges, threadRuns, tokenTotals,
} from './assistant'
import { emptySelection, type AgentRun, type Proposal, type ReviewIssue, type Row } from './types'

const run = (id: string, created_at: string, extra: Partial<AgentRun> = {}): AgentRun => ({
  id, kind: 'correction', input_revision: 'rev-1', selection: null, instruction: id, provider: 'p', model: 'm',
  skill_version: '', status: 'succeeded', progress: [], outcome: {}, error: null, created_at, finished_at: created_at,
  ...extra,
})

describe('assistant thread', () => {
  it('totals all recorded runs and live usage without double counting', () => {
    expect(tokenTotals({ old: { input_tokens: 100, output_tokens: 10 }, a: { input_tokens: 20, output_tokens: 2 } }, {
      a: run('a', '1', { outcome: { input_tokens: 30, output_tokens: 3 } }),
      auto: run('auto', '2', { kind: 'autofix', outcome: { input_tokens: 50, output_tokens: 5 } }),
      pending: run('pending', '3', { status: 'queued' }),
    })).toEqual({ input_tokens: 180, output_tokens: 18 })
  })
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

describe('shared issue prompt context', () => {
  const message = 'The required outlet connection point is missing. '.repeat(12)
  const makeIssue = (id: string, extra = {}): ReviewIssue => ({
    id, affected_ids: [id], category: 'validation', severity: 'violation', origin: 'validation',
    resolution_state: 'open', explanation: `${id}: ${message}`,
    details: { findings: [{ message, shape: 'urn:shape:outlet', path: 's223:hasConnectionPoint',
      severity: 'Violation', focus: `urn:plant:${id}`, ...extra }] },
  })

  it('shares validator text and metadata while retaining every issue and focus', () => {
    const issues = Array.from({ length: 20 }, (_, i) => makeIssue(`eq-${i}`))
    const prompt = issuesPrompt(issues, new Map())
    for (const issue of issues) expect(prompt).toContain(`[${issue.id}] Affected: ${issue.id}; Focus: urn:plant:${issue.id}`)
    expect(prompt.split(message).length - 1).toBe(1)
    expect(prompt.match(/Path:/g)).toHaveLength(1)
    expect(prompt.length).toBeLessThan(issues.map((i) => issuesPrompt([i], new Map())).join('\n').length / 2)
    expect(prompt).not.toContain('"groups"')
  })

  it('keeps different paths, shapes, values and severity distinct', () => {
    const prompt = issuesPrompt([makeIssue('a', { value: 'wrong-a', statement_id: 'statement-a' }),
      makeIssue('b', { path: 's223:hasProperty', value: 'wrong-b' }),
      makeIssue('c', { shape: 'urn:shape:other', severity: 'Warning' })], new Map())
    expect(prompt).toContain('Path: s223:hasConnectionPoint')
    expect(prompt).toContain('Path: s223:hasProperty')
    expect(prompt).toContain('Shape: urn:shape:other')
    expect(prompt).toContain('Finding severity: Warning')
    expect(prompt).toContain('Statement: statement-a')
    expect(prompt).toContain('[a] Affected: a; Focus: urn:plant:a; Value: "wrong-a"')
    expect(prompt).toContain('[b] Affected: b; Focus: urn:plant:b; Value: "wrong-b"')
  })

  it('shares paths and shapes even when the validator messages differ', () => {
    const prompt = issuesPrompt([makeIssue('a'), makeIssue('b', { message: 'An incompatible outlet exists instead.' })], new Map())
    expect(prompt.match(/Path:/g)).toHaveLength(1)
    expect(prompt.match(/Shape:/g)).toHaveLength(1)
    expect(prompt).toContain(message)
    expect(prompt).toContain('An incompatible outlet exists instead.')
  })

  it('deduplicates issue IDs and preserves non-validation explanations and details', () => {
    const other = { ...makeIssue('other'), origin: 'association', category: 'unassigned',
      severity: 'warning' as const, explanation: 'This sensor may belong to either tank.',
      details: { candidates: ['eq-a', 'eq-b'] } }
    const prompt = issuesPrompt([makeIssue('a'), makeIssue('a'), other], new Map())
    expect(prompt.match(/\[a\] Affected:/g)).toHaveLength(1)
    expect(prompt).toContain(other.explanation)
    expect(prompt).toContain('Details: {"candidates":["eq-a","eq-b"]}')
  })

  it('also factors repeated non-validation explanations without merging different objects', () => {
    const a = { ...makeIssue('a'), origin: 'association', details: {}, explanation: 'Unknown equipment association' }
    const b = { ...a, id: 'b', affected_ids: ['b'] }
    const prompt = issuesPrompt([a, b], new Map())
    expect(prompt.split(a.explanation).length - 1).toBe(1)
    expect(prompt).toContain('[a] Affected: a')
    expect(prompt).toContain('[b] Affected: b')
  })

  it('compacts the reported eight inlet/outlet issues into readable grouped rules', () => {
    const equipment = [
      ['eq-4c0af5', 'A1', 'Damper', 'val-b3055977bde8', 'val-eaa961afaa5c'],
      ['eq-dbb83c', 'A1E', 'Damper', 'val-b4692d9cf482', 'val-e98d64cdee5e'],
      ['eq-178e00', 'A2', 'Damper', 'val-8bb9c04c0cb3', 'val-2b3225c783dd'],
      ['eq-9033e6', 'AHU', 'AirHandlingUnit', 'val-6e5d75ef14c5', 'val-c321b2add2ad'],
    ]
    const rows = new Map(equipment.map(([id, label]) => [id, {id, label, kind:'equipment', iri:`urn:workbench:t2-3ae6/${id}`}])) as Map<string, Row>
    const issues = equipment.flatMap(([id, , shape, inlet, outlet]) => ['inlet', 'outlet'].map((direction, i) => {
      const focus = rows.get(id)!.iri
      const message = `s223: ${shape === 'Damper' ? 'A' : 'An'} \`${shape}\` shall have at least one ${direction} using the medium \`Fluid-Air\`.`
      return {...makeIssue(i ? outlet : inlet), affected_ids:[id], details:{findings:[{focus, value:focus,
        message, severity:'Violation', path:'http://data.ashrae.org/standard223#hasConnectionPoint',
        shape:`http://data.ashrae.org/standard223#${shape}`, statement_id: shape === 'Damper' ? 196 : 107}]}}
    }))
    const prompt = issuesPrompt(issues, rows)
    expect(prompt.length).toBeLessThan(2100)
    expect(prompt.match(/hasConnectionPoint/g)).toHaveLength(1)
    expect(prompt.match(/shall have at least one/g)).toHaveLength(4)
    expect(prompt.match(/Affected:/g)).toHaveLength(8)
    expect(prompt.match(/Statement: 196/g)).toHaveLength(1)
    expect(prompt.match(/Statement: 107/g)).toHaveLength(1)
    expect(prompt).toContain("Focus: first affected object's IRI; Value: same as focus")
    for (const issue of issues) expect(prompt).toContain(`[${issue.id}] Affected: ${issue.affected_ids[0]}`)
    for (const row of rows.values()) expect(prompt.split(row.iri).length - 1).toBe(1)
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

  it('reads an auto-fix run: progress while running, the report when done', () => {
    const ev = (stage: string, message: string, data: Record<string, unknown>) => ({ at: '', stage, message, data })
    const running = { progress: [ev('autofix', '3 issues in 2 groups', { groups: 2 }), ev('group', 'Group 1/2', { group: 1 }),
      ev('group_done', 'Group 1/2: fixed', { group: 1, status: 'fixed', reasons: [] }), ev('group', 'Group 2/2', { group: 2 }),
      ev('model', '2/2 · Asking', { group: 2 })], outcome: {} } as unknown as AgentRun
    expect(autofixProgress(running)).toEqual({ group: 2, total: 2, message: '2/2 · Asking' })
    expect(autofixReport(running).groups.map((g) => g.status)).toEqual(['fixed'])
    const done = { ...running, outcome: { autofix: { groups: [{ issues: ['a'], explanations: ['x'], status: 'review' }], revisions: ['rev-2'] } } }
    expect(autofixReport(done as AgentRun)).toEqual(done.outcome.autofix)
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
    expect(lines[1].detail).toBe('maps to: none → AHU inlet; paired with: none → none (+1 more)')
  })
})

describe('auto-fix on a selection', () => {
  it('offers nothing when the selection has no issues (not every violation)', () => {
    const issue = { id: 'a', affected_ids: ['eq-1'], category: 'validation', severity: 'violation', explanation: 'x',
      resolution_state: 'open', origin: 'validation', details: {} } as ReviewIssue
    expect(issuesOnSelection([issue], { ...emptySelection(), entity_ids: ['https://example.org/term'] })).toEqual([])
  })
})
