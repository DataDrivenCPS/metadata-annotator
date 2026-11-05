import { describe, expect, it } from 'vitest'
import type { AutofixGroup } from './assistant'
import { affectedRows, groupIssues, headline, isHandled, labelList, reportSections, tally } from './autofixView'
import type { ReviewIssue, Row } from './types'

const group = (status: AutofixGroup['status'], extra: Partial<AutofixGroup> = {}): AutofixGroup =>
  ({ issues: ['i1'], explanations: [`${status} issue`], status, ...extra })
const choice = (ids: string[]) => ({ question: 'Which medium for P-1?', options: ids.map((id) => ({ label: id, proposal_id: id })) })

describe('auto-fix report layout', () => {
  it('puts what needs the person first and keeps each group in its place in the run', () => {
    const sections = reportSections([group('fixed'), group('review'), group('choice'), group('fixed'), group('resolved')])
    expect(sections.map((s) => s.status)).toEqual(['choice', 'review', 'fixed', 'resolved'])
    expect(sections.find((s) => s.status === 'fixed')!.items.map((i) => i.key)).toEqual([0, 3])
  })

  it('names an item by the question it asks, else its first issue', () => {
    expect(headline(group('choice', { choices: [choice(['a'])] }))).toBe('Which medium for P-1?')
    expect(headline(group('input', { questions: ['Is P-1 a pump?'] }))).toBe('Is P-1 a pump?')
    expect(headline(group('input'))).toBe('input issue')
    expect(headline(group('failed', { explanations: [], reasons: ['boom'] }))).toBe('boom')
  })

  it('counts a choice as handled once each question is answered or withdrawn, and a review once decided', () => {
    const g = group('choice', { choices: [choice(['a', 'b']), choice(['c'])] })
    expect(isHandled(g, { a: 'pending', b: 'pending', c: 'pending' })).toBe(false)
    expect(isHandled(g, { a: 'applied', b: 'dismissed', c: 'pending' })).toBe(false)
    expect(isHandled(g, { a: 'applied', b: 'dismissed', c: 'dismissed' })).toBe(true)
    expect(isHandled(group('review', { proposal_id: 'p' }), { p: 'stale' })).toBe(false)
    expect(isHandled(group('review', { proposal_id: 'p' }), { p: 'applied' })).toBe(true)
    expect(isHandled(group('input'), {})).toBe(false)
    expect(tally([g, group('fixed'), group('input')], { a: 'applied', b: 'dismissed', c: 'dismissed' }))
      .toEqual({ fixed: 1, forYou: 2, handled: 1 })
  })

  it('lists the affected objects that are in the model, once each', () => {
    const issue = (id: string, affected: string[]) => ({ id, affected_ids: affected }) as ReviewIssue
    const rows = new Map<string, Row>([['eq-1', { id: 'eq-1', label: 'RO-1' } as Row], ['eq-2', { id: 'eq-2', label: 'RO-2' } as Row],
      ['eq-3', { id: 'eq-3', label: 'RO-3' } as Row]])
    const g = group('review', { issues: ['i1', 'i2'] })
    const mine = groupIssues(g, [issue('i1', ['eq-1', 'gone']), issue('i2', ['eq-1', 'eq-2', 'eq-3']), issue('i3', ['eq-9'])])
    expect(mine.map((i) => i.id)).toEqual(['i1', 'i2'])
    const found = affectedRows(mine, rows)
    expect(found.map((r) => r.id)).toEqual(['eq-1', 'eq-2', 'eq-3'])
    expect(labelList(found)).toBe('RO-1, RO-2 +1')
  })
})
