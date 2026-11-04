import { describe, expect, it } from 'vitest'
import { boxPercent, citationBox, citationLabel, citationSummary, excerpt, isGenerated, locatePassage, recordsToShow, zoomToBox } from './citations'
import type { Citation, Proposal } from './types'

const cite = (content: Citation['content'], location: Partial<Citation['location']> = {}): Citation =>
  ({ id: 'obs-1', source_id: 'src-1', kind: 'label', status: 'unresolved', entity_ids: [], content,
     location: { kind: 'document', ...location } })

describe('citation label', () => {
  it('names the file, page and the end of the section path', () => {
    expect(citationLabel(cite({ filename: 'plant.pdf', section: ['Plant', 'Pumps', 'Duty'] }, { page: 3 })))
      .toBe('plant.pdf · page 3 · Pumps > Duty')
  })
  it('counts rows and columns from one', () => {
    expect(citationLabel(cite({ filename: 'points.csv' }, { kind: 'csv_row', row: 0 }))).toBe('points.csv · row 1')
    expect(citationLabel(cite({ filename: 'points.csv' }, { kind: 'csv_column', column: 4 }))).toBe('points.csv · column 5')
  })
})

describe('citation box', () => {
  it('prefers the PDF box and falls back to the image region', () => {
    expect(citationBox(cite({ box_pt: [1, 2, 3, 4] }))).toEqual([1, 2, 3, 4])
    expect(citationBox(cite({}, { bbox: [5, 6, 7, 8] }))).toEqual([5, 6, 7, 8])
  })
  it('ignores anything that is not a box', () => {
    expect(citationBox(cite({ box_pt: [1, 2] }))).toBeNull()
    expect(citationBox(cite({}))).toBeNull()
  })
  it('places a box on the page in percentages', () => {
    expect(boxPercent([50, 100, 25, 50], 100, 200)).toEqual({ left: '50%', top: '50%', width: '25%', height: '25%' })
  })
  it('centres the box and never zooms out past the whole image', () => {
    const near = zoomToBox([400, 300, 100, 100], 600, 600, 1000, 1000)
    expect(near.scale).toBe(4)
    expect(near.x + 450 * near.scale).toBe(300)
    expect(near.y + 350 * near.scale).toBe(300)
    expect(zoomToBox([0, 0, 1000, 1000], 600, 600, 1000, 1000).scale).toBe(0.6)
  })
})

describe('locating a passage', () => {
  const text = 'Intro paragraph.\nPump P-1 feeds tank TK-2.\nSome more text follows here, enough to fill the page out.'
  const start = text.indexOf('Pump')
  const passage = 'Pump P-1 feeds tank TK-2.'
  it('uses stored offsets when they still hold the passage', () => {
    expect(locatePassage(text, cite({ text: passage }, { text_range: [start, start + passage.length] })))
      .toEqual([start, start + passage.length])
  })
  it('allows for whitespace differences at the stored offsets', () => {
    const wrapped = 'Intro.\nPump P-1\nfeeds tank TK-2.\nThen a good deal more text so this is not the whole page at all.'
    const at = wrapped.indexOf('Pump')
    expect(locatePassage(wrapped, cite({ text: passage }, { text_range: [at, at + 'Pump P-1\nfeeds tank TK-2.'.length] })))
      .toEqual([at, at + 'Pump P-1\nfeeds tank TK-2.'.length])
  })
  it('looks for the passage when the offsets do not hold it', () => {
    expect(locatePassage(text, cite({ text: passage }, { text_range: [0, 5] }))).toEqual([start, start + passage.length])
  })
  it('highlights nothing when the source no longer says it', () => {
    expect(locatePassage(text, cite({ text: 'Pump P-9 feeds nothing.' }, { text_range: [start, start + 23] }))).toBeNull()
  })
  it('does not call a whole page a passage', () => {
    expect(locatePassage(text, cite({ text }))).toBeNull()
  })
  it('never locates a model-written description', () => {
    const generated = cite({ text: passage, generated: true })
    expect(isGenerated(generated)).toBe(true)
    expect(locatePassage(text, generated)).toBeNull()
  })
})

describe('excerpt', () => {
  it('leaves short text alone', () => {
    expect(excerpt('short')).toEqual({ text: 'short', truncated: false })
  })
  it('shortens long text and says so', () => {
    const long = excerpt('x'.repeat(500))
    expect(long.truncated).toBe(true)
    expect(long.text.length).toBe(401)
  })
})

describe('records to show', () => {
  const records = Array.from({ length: 5 }, (_, i) => ({ id: `r${i}` }))
  it('keeps the limit when the cited record is inside it', () => {
    expect(recordsToShow(records, 'r1', 2).map((r) => r.id)).toEqual(['r0', 'r1'])
  })
  it('adds a cited record past the limit', () => {
    expect(recordsToShow(records, 'r4', 2).map((r) => r.id)).toEqual(['r0', 'r1', 'r4'])
  })
  it('ignores a cited record that is not listed', () => {
    expect(recordsToShow(records, 'gone', 2).map((r) => r.id)).toEqual(['r0', 'r1'])
  })
})

describe('citation summary', () => {
  const change = (entity_id: string, kind: 'created' | 'deleted' = 'created') =>
    ({ entity_id, entity_kind: 'equipment', label: entity_id, change: kind, fields: [], in_selection: true, overrides_locked: [] })
  it('counts changes that cite a source, and evidence read but not cited', () => {
    const proposal = {
      operations: [{ op: 'create_equipment', id: 'eq-1', evidence: ['obs-1'] }, { op: 'create_equipment', id: 'eq-2' },
        { op: 'delete_equipment', id: 'eq-3' }],
      changes: [change('eq-1'), change('eq-2'), change('eq-3', 'deleted')],
      evidence: [{ kind: 'observation', ref: 'obs-1', summary: '' }, { kind: 'observation', ref: 'obs-2', summary: '' },
        { kind: 'observation', ref: 'obs-2', summary: '' }, { kind: 'model', ref: 'eq-2', summary: '' }],
    } as unknown as Proposal
    expect(citationSummary(proposal)).toEqual({ cited: 1, total: 2, read: 1 })
  })
})
