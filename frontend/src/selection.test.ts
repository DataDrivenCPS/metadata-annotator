import { describe, expect, it } from 'vitest'
import { applyClick, filterRows, pruneSelection, sortRows, summarize, type Column } from './selection'
import { emptySelection, type PointRow, type Row } from './types'

const pt = (id: string, label: string, equipment: string | null): PointRow => ({
  kind: 'point', id, iri: `urn:x/${id}`, label, point_kind: 'measurement', point_kind_label: 'Measurement',
  quantity_kind: null, unit: null, unit_symbol: '', equipment: equipment ? { id: `eq-${equipment}`, label: equipment } : null,
  medium: null, substance: null, sensor_type: null, locked: [], evidence: [], point_type: null,
})

// Two points share a label: a duplicate in the source must stay two records.
const rows = [pt('pt-a', 'PT-201', 'P-201'), pt('pt-b', 'FT-201', 'P-201'), pt('pt-c', 'PT-201', 'RO-1'), pt('pt-d', 'AT-101', 'P-101')]
const cols: Column<PointRow>[] = [
  { key: 'label', header: 'Point', value: (r) => r.label },
  { key: 'equipment', header: 'Equipment', field: 'equipment', value: (r) => r.equipment?.label ?? '' },
]
const none = { ctrl: false, shift: false }

describe('selection by stable id', () => {
  it('survives sorting and filtering', () => {
    const shown = rows.map((r) => r.id)
    let sel = applyClick(emptySelection(), { id: 'pt-c' }, none, shown, null)
    sel = applyClick(sel, { id: 'pt-b' }, { ctrl: true, shift: false }, shown, 'pt-c')
    const sorted = sortRows(rows, cols, { key: 'label', dir: -1 })
    const filtered = filterRows(sorted, cols, 'P-201')
    expect(sel.entity_ids.sort()).toEqual(['pt-b', 'pt-c'])
    // the display changed, the selection did not
    expect(filtered.map((r) => r.id)).not.toContain('pt-c')
    expect(sel.entity_ids).toContain('pt-c')
  })

  it('keeps duplicate labels as distinct selections', () => {
    const shown = rows.map((r) => r.id)
    const sel = applyClick(emptySelection(), { id: 'pt-a' }, none, shown, null)
    expect(sel.entity_ids).toEqual(['pt-a'])
    const sorted = sortRows(rows, cols, { key: 'label', dir: 1 })
    const dupes = sorted.filter((r) => r.label === 'PT-201').map((r) => r.id)
    expect(dupes).toEqual(['pt-a', 'pt-c']) // id tiebreak keeps order stable
  })

  it('shift-selects a range in display order, not model order', () => {
    const sorted = sortRows(rows, cols, { key: 'label', dir: 1 }) // AT-101, FT-201, PT-201(a), PT-201(c)
    const shown = sorted.map((r) => r.id)
    let sel = applyClick(emptySelection(), { id: 'pt-d' }, none, shown, null)
    sel = applyClick(sel, { id: 'pt-a' }, { ctrl: false, shift: true }, shown, 'pt-d')
    expect(sel.entity_ids).toEqual(['pt-d', 'pt-b', 'pt-a'])
  })

  it('records field scope from cell clicks and summarizes it', () => {
    const shown = rows.map((r) => r.id)
    let sel = applyClick(emptySelection(), { id: 'pt-a', field: 'equipment' }, none, shown, null)
    sel = applyClick(sel, { id: 'pt-b', field: 'equipment' }, { ctrl: true, shift: false }, shown, 'pt-a')
    const byId = new Map<string, Row>(rows.map((r) => [r.id, r]))
    expect(sel.field_ids).toEqual(['equipment'])
    expect(summarize(sel, byId)).toBe('Equipment assignments for 2 points')
  })

  it('names connection points in plain words', () => {
    const cp = { kind: 'connection_point', id: 'cp-1', label: 'HX inlet' } as unknown as Row
    const sel = { ...emptySelection(), entity_ids: ['cp-1'], field_ids: ['paired_with'] }
    expect(summarize(sel, new Map([['cp-1', cp]]))).toBe('Pairings for 1 connection point')
  })

  it('keeps entity and relationship selections separate', () => {
    let sel = applyClick(emptySelection(), { id: 'eq-1' }, none, [], null)
    sel = applyClick(sel, { id: 'cx-1', relationship: true }, { ctrl: true, shift: false }, [], null)
    expect(sel.entity_ids).toEqual(['eq-1'])
    expect(sel.relationship_ids).toEqual(['cx-1'])
    sel = applyClick(sel, { id: 'cx-2', relationship: true }, none, [], null)
    expect(sel.entity_ids).toEqual([])
    expect(sel.relationship_ids).toEqual(['cx-2'])
  })

  it('prunes ids that no longer exist', () => {
    const sel = { ...emptySelection(), entity_ids: ['pt-a', 'pt-x'], field_ids: ['unit'] }
    expect(pruneSelection(sel, new Set(['pt-a'])).entity_ids).toEqual(['pt-a'])
    expect(pruneSelection(sel, new Set()).field_ids).toEqual([])
  })
})
