import { describe, expect, it } from 'vitest'
import {
  addConnection, addEquipment, assignPorts, compileDraft, draftConnectionRows, draftCount, draftEquipmentRows,
  emptyDraft, portOptions, removeItem, setField, setPort, type Draft,
} from './draft'
import type { ConnectionPointRow, ConnectionRow, EquipmentRow, ModelResponse } from './types'

const WATER = { iri: 'urn:s223#Fluid-Water', label: 'Water' }
const BRINE = { iri: 'urn:watr#Water-Brine', label: 'Brine' }
const TANK = { iri: 'urn:watr#Tank', label: 'Tank' }

const eq = (id: string, label: string): EquipmentRow => ({
  kind: 'equipment', id, iri: `urn:m#${id}`, label, type: TANK, process: null, contained_in: null,
  point_count: 0, locked: [], evidence: [], location: null,
})
const cx = (id: string, from: string, to: string): ConnectionRow => ({
  kind: 'connection', id, iri: `urn:m#${id}`, label: id, type: null, medium: WATER, directed: true, locked: [], evidence: [],
  from_equipment: { id: from, label: from }, to_equipment: { id: to, label: to }, from_point: null, to_point: null,
})
const port = (id: string, equipment: string, direction: ConnectionPointRow['direction'], medium = WATER, connection: string | null = null): ConnectionPointRow => ({
  kind: 'connection_point', id, iri: `urn:m#${id}`, label: id, equipment: { id: equipment, label: equipment }, direction,
  medium, connection: connection ? { id: connection, label: connection } : null, paired_with: null, maps_to: null,
  mapped_from: null, locked: [], evidence: [],
})

function model(family: 's223' | 'brick' = 's223', ports: ConnectionPointRow[] = []): ModelResponse {
  return {
    revision: { id: 'rev-1', parent_id: null, created_at: '', author: 'user', kind: 'edit', summary: '', proposal_id: null, validation: null },
    head: 'rev-1',
    info: { id: 'p', name: 'p', namespace: 'urn:m#', head: 'rev-1', profile: family === 's223' ? 'watr' : 'brick',
      profile_label: '', family, can_undo: false, can_redo: false, updated_at: '' },
    view: { equipment: [eq('eq-a', 'TK-1'), eq('eq-b', 'RO-1')], points: [], connections: [cx('cx-1', 'eq-a', 'eq-b')],
      connection_points: ports, spaces: [], entities: [], relationships: [], containment: [] },
    issues: [], layout: {},
  }
}

describe('graph drafts', () => {
  it('compiles new equipment, then new connections, then edits, with placeholders', () => {
    const m = model()
    let d: Draft = emptyDraft()
    let uv: string, line: string
    ;[d, uv] = addEquipment(d, [10, 20], 'UV-1')
    d = setField(m, d, uv, 'type', { value: TANK.iri, label: TANK.label })
    ;[d, line] = addConnection(d, 'eq-a', uv)
    d = setField(m, d, line, 'medium', { value: WATER.iri, label: WATER.label })
    d = setField(m, d, 'eq-b', 'label', { value: 'RO-1 Membrane Skid', label: 'RO-1 Membrane Skid' })
    const { ops, problems } = compileDraft(m, d)
    expect(problems).toEqual([])
    expect(ops).toEqual([
      { op: 'create_equipment', id: uv, label: 'UV-1', type: TANK.iri },
      { op: 'create_connection', id: line, from_equipment: 'eq-a', to_equipment: uv, medium: WATER.iri },
      { op: 'update_equipment', id: 'eq-b', label: 'RO-1 Membrane Skid' },
    ])
    expect(uv.startsWith('new:') && line.startsWith('new:')).toBe(true)
    expect(draftCount(d)).toBe(3)
  })

  it('merges repeated edits of one row and drops edits that are set back', () => {
    const m = model()
    let d = setField(m, emptyDraft(), 'cx-1', 'medium', { value: BRINE.iri, label: BRINE.label })
    d = setField(m, d, 'cx-1', 'label', { value: 'L-06', label: 'L-06' })
    d = setField(m, d, 'cx-1', 'to_equipment', { value: 'eq-a', label: 'TK-1' })
    expect(compileDraft(m, d).ops).toEqual([{ op: 'update_connection', id: 'cx-1', medium: BRINE.iri, label: 'L-06', to_equipment: 'eq-a' }])
    expect(compileDraft(m, d).problems).toEqual([{ id: 'cx-1', message: 'connects equipment to itself' }])
    d = setField(m, d, 'cx-1', 'medium', { value: WATER.iri, label: WATER.label })
    d = setField(m, d, 'cx-1', 'label', { value: 'cx-1', label: 'cx-1' })
    d = setField(m, d, 'cx-1', 'to_equipment', { value: 'eq-b', label: 'RO-1' })
    expect(d.edits).toEqual({})
  })

  it('reports what must be settled before saving', () => {
    const m = model()
    let [d, key] = addEquipment(emptyDraft(), [0, 0])
    d = setField(m, d, key, 'label', { value: '  ', label: '' })
    let line: string
    ;[d, line] = addConnection(d, 'eq-a', 'eq-b')
    expect(compileDraft(m, d).problems).toEqual([
      { id: key, message: 'needs a name' }, { id: key, message: 'needs an equipment type' },
      { id: line, message: 'needs a medium (what the pipe or duct carries)' },
    ])
  })

  it('Brick connections need no medium and carry no connection points', () => {
    const m = model('brick')
    const [d, line] = addConnection(emptyDraft(), 'eq-a', 'eq-b')
    expect(compileDraft(m, d)).toEqual({ problems: [], ops: [{ op: 'create_connection', id: line, from_equipment: 'eq-a', to_equipment: 'eq-b' }] })
  })

  it('reuses the one free matching connection point and mints one otherwise', () => {
    const m = model('s223', [
      port('cp-a-out', 'eq-a', 'outlet'), port('cp-a-used', 'eq-a', 'outlet', WATER, 'cx-1'),
      port('cp-b-in1', 'eq-b', 'inlet'), port('cp-b-in2', 'eq-b', 'bidirectional'),
    ])
    let [d, line] = addConnection(emptyDraft(), 'eq-a', 'eq-b')
    // the medium comes from the reused point, so none has to be chosen
    expect(compileDraft(m, d)).toEqual({ problems: [], ops: [
      { op: 'create_connection', id: line, from_equipment: 'eq-a', to_equipment: 'eq-b', from_point: 'cp-a-out' }] })
    // a second connection from the same equipment can no longer take that point
    let second: string
    ;[d, second] = addConnection(d, 'eq-a', 'eq-b')
    expect(assignPorts(m, d).get(second)).toEqual({ from: null, to: null })
    // choosing a point explicitly claims it before automatic ends are assigned
    d = setPort(d, second, 'to', 'cp-b-in2')
    expect(assignPorts(m, d).get(line)!.to?.id).toBe('cp-b-in1')
    expect(portOptions(m, d, line, 'to').map((p) => p.id)).toEqual(['cp-b-in1'])
    d = setPort(d, line, 'from', 'new')
    expect(assignPorts(m, d).get(line)!.from).toBeNull()
    expect(assignPorts(m, d).get(second)!.from?.id).toBe('cp-a-out')
  })

  it('a different medium keeps a free point from being reused', () => {
    const m = model('s223', [port('cp-a-out', 'eq-a', 'outlet', WATER)])
    let [d, line] = addConnection(emptyDraft(), 'eq-a', 'eq-b')
    d = setField(m, d, line, 'medium', { value: BRINE.iri, label: BRINE.label })
    expect(assignPorts(m, d).get(line)).toEqual({ from: null, to: null })
  })

  it('overlays the draft on the model rows for drawing', () => {
    const m = model()
    let [d, uv] = addEquipment(emptyDraft(), [5, 5], 'UV-1')
    d = setField(m, d, 'eq-a', 'label', { value: 'TK-1 Feed Tank', label: 'TK-1 Feed Tank' })
    let line: string
    ;[d, line] = addConnection(d, 'eq-a', uv)
    expect(draftEquipmentRows(m, d).map((e) => [e.id, e.label])).toEqual([['eq-a', 'TK-1 Feed Tank'], ['eq-b', 'RO-1'], [uv, 'UV-1']])
    const rows = draftConnectionRows(m, d)
    expect(rows.find((r) => r.id === line)).toMatchObject({ label: 'TK-1 Feed Tank → UV-1', to_equipment: { id: uv, label: 'UV-1' } })
    expect(rows.find((r) => r.id === 'cx-1')!.from_equipment).toEqual({ id: 'eq-a', label: 'TK-1 Feed Tank' })
    expect(m.view.equipment[0].label).toBe('TK-1')  // the model itself is untouched
  })

  it('removing new equipment takes its connections and the edits that point at it', () => {
    const m = model()
    let [d, uv] = addEquipment(emptyDraft(), [0, 0], 'UV-1')
    ;[d] = addConnection(d, uv, 'eq-b')
    d = setField(m, d, 'cx-1', 'to_equipment', { value: uv, label: 'UV-1' })
    d = setField(m, d, 'cx-1', 'label', { value: 'L-01', label: 'L-01' })
    d = removeItem(d, uv)
    expect(d.equipment).toEqual([])
    expect(d.connections).toEqual([])
    expect(d.edits).toEqual({ 'cx-1': { label: { value: 'L-01', label: 'L-01' } } })
    expect(draftCount(removeItem(d, 'cx-1'))).toBe(0)
  })
})
