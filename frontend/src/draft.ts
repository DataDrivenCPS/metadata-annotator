// Draft mode: changes drawn on the graph and saved together as one edit.
// Pure functions over the current model, unit-tested in draft.test.ts. Nothing here talks to the
// server: a draft becomes ordinary operations (compileDraft) only when it is checked or saved, and
// the model itself is never edited in the browser — the graph only overlays the draft on it.

import type { ConnectionPointRow, ConnectionRow, EquipmentRow, ModelResponse, TermRef } from './types'

/** Draft objects are named by placeholders, which the server swaps for minted ids when it saves. */
export const NEW = 'new:'
export const isDraftId = (id: string) => id.startsWith(NEW)

export interface DraftTerm { iri: string; label: string }
/** A new connection's end: ``auto`` joins the equipment's only free matching connection point,
 * else mints one; ``new`` always mints one; otherwise the id of the connection point to join. */
export type PortChoice = 'auto' | 'new' | (string & {})

export interface NewEquipment {
  key: string; label: string; type: DraftTerm | null; process: DraftTerm | null; position: [number, number]
}
export interface NewConnection {
  key: string; from: string; to: string; label: string
  medium: DraftTerm | null; type: DraftTerm | null; from_point: PortChoice; to_point: PortChoice
}
/** A drafted field: what the operation sends (term IRI, entity id or text) and what to show. */
export interface FieldValue { value: string | null; label: string }
export type EquipmentField = 'label' | 'type' | 'process'
export type ConnectionField = 'label' | 'from_equipment' | 'to_equipment' | 'medium' | 'type'
export type DraftField = EquipmentField | ConnectionField

export interface Draft {
  seq: number
  equipment: NewEquipment[]
  connections: NewConnection[]
  /** Changed fields of equipment and connections already in the model, by id. */
  edits: Record<string, Partial<Record<DraftField, FieldValue>>>
}

export interface DraftProblem { id: string; message: string }

export const emptyDraft = (): Draft => ({ seq: 0, equipment: [], connections: [], edits: {} })

export const draftCount = (d: Draft) => d.equipment.length + d.connections.length + Object.keys(d.edits).length

const nextKey = (d: Draft, kind: 'eq' | 'cx') => `${NEW}${kind}-${d.seq + 1}`

export function addEquipment(d: Draft, position: [number, number], label = 'New equipment'): [Draft, string] {
  const key = nextKey(d, 'eq')
  return [{ ...d, seq: d.seq + 1, equipment: [...d.equipment, { key, label, type: null, process: null, position }] }, key]
}

export function addConnection(d: Draft, from: string, to: string): [Draft, string] {
  const key = nextKey(d, 'cx')
  const cx: NewConnection = { key, from, to, label: '', medium: null, type: null, from_point: 'auto', to_point: 'auto' }
  return [{ ...d, seq: d.seq + 1, connections: [...d.connections, cx] }, key]
}

export function moveEquipment(d: Draft, key: string, position: [number, number]): Draft {
  return { ...d, equipment: d.equipment.map((e) => (e.key === key ? { ...e, position } : e)) }
}

/** The field's value in the model (before the draft): an IRI, an id or the label. */
function modelValue(model: ModelResponse, id: string, field: DraftField): string | null | undefined {
  const eq = model.view.equipment.find((e) => e.id === id)
  if (eq) {
    if (field === 'label') return eq.label
    if (field === 'type') return eq.type?.iri ?? null
    if (field === 'process') return eq.process?.iri ?? null
    return undefined
  }
  const cx = model.view.connections.find((c) => c.id === id)
  if (!cx) return undefined
  if (field === 'label') return cx.label
  if (field === 'type') return cx.type?.iri ?? null
  if (field === 'medium') return cx.medium?.iri ?? null
  if (field === 'from_equipment') return cx.from_equipment?.id ?? null
  if (field === 'to_equipment') return cx.to_equipment?.id ?? null
  return undefined
}

const term = (v: FieldValue): DraftTerm | null => (v.value === null ? null : { iri: v.value, label: v.label })

/** Set one field of a draft object or of an existing row. Setting an existing row's field back
 * to what the model has drops that edit, so a draft never holds changes that change nothing. */
export function setField(model: ModelResponse, d: Draft, id: string, field: DraftField, v: FieldValue): Draft {
  if (d.equipment.some((e) => e.key === id)) {
    return { ...d, equipment: d.equipment.map((e) => {
      if (e.key !== id) return e
      if (field === 'label') return { ...e, label: v.value ?? '' }
      if (field === 'type') return { ...e, type: term(v) }
      if (field === 'process') return { ...e, process: term(v) }
      return e
    }) }
  }
  if (d.connections.some((c) => c.key === id)) {
    return { ...d, connections: d.connections.map((c) => {
      if (c.key !== id) return c
      if (field === 'label') return { ...c, label: v.value ?? '' }
      if (field === 'medium') return { ...c, medium: term(v) }
      if (field === 'type') return { ...c, type: term(v) }
      // a moved end joins whatever fits at its new equipment
      if (field === 'from_equipment' && v.value) return { ...c, from: v.value, from_point: 'auto' }
      if (field === 'to_equipment' && v.value) return { ...c, to: v.value, to_point: 'auto' }
      return c
    }) }
  }
  const current = modelValue(model, id, field)
  if (current === undefined) return d
  const fields = { ...d.edits[id] }
  if (current === v.value) delete fields[field]
  else fields[field] = v
  const edits = { ...d.edits }
  if (Object.keys(fields).length) edits[id] = fields
  else delete edits[id]
  return { ...d, edits }
}

export function setPort(d: Draft, key: string, end: 'from' | 'to', choice: PortChoice): Draft {
  return { ...d, connections: d.connections.map((c) => (c.key === key ? { ...c, [`${end}_point`]: choice } : c)) }
}

/** Remove a draft object (a new equipment takes its new connections along) or an existing row's edits. */
export function removeItem(d: Draft, id: string): Draft {
  if (d.equipment.some((e) => e.key === id)) {
    const edits = Object.fromEntries(Object.entries(d.edits).map(([eid, f]) => [eid, Object.fromEntries(
      Object.entries(f).filter(([k, v]) => !((k === 'from_equipment' || k === 'to_equipment') && v?.value === id)))])
      .filter(([, f]) => Object.keys(f).length))
    return { ...d, equipment: d.equipment.filter((e) => e.key !== id),
      connections: d.connections.filter((c) => c.from !== id && c.to !== id), edits }
  }
  if (d.connections.some((c) => c.key === id)) return { ...d, connections: d.connections.filter((c) => c.key !== id) }
  const edits = { ...d.edits }
  delete edits[id]
  return { ...d, edits }
}

// ------------------------------------------------------------------ overlay

/** Equipment as the draft would leave it: edited rows plus new ones (ids are placeholders). */
export function draftEquipmentRows(model: ModelResponse, d: Draft): EquipmentRow[] {
  const rows = model.view.equipment.map((r) => {
    const f = d.edits[r.id]
    if (!f) return r
    return { ...r, ...(f.label ? { label: f.label.value ?? '' } : {}),
      ...(f.type ? { type: term(f.type) as TermRef | null } : {}), ...(f.process ? { process: term(f.process) as TermRef | null } : {}) }
  })
  return [...rows, ...d.equipment.map((e): EquipmentRow => ({
    kind: 'equipment', id: e.key, iri: '', label: e.label, type: e.type, process: e.process, contained_in: null,
    point_count: 0, locked: [], evidence: [], location: null,
  }))]
}

/** Connections as the draft would leave them. */
export function draftConnectionRows(model: ModelResponse, d: Draft): ConnectionRow[] {
  const labels = new Map(draftEquipmentRows(model, d).map((e) => [e.id, e.label]))
  const ref = (id: string | null) => (id ? { id, label: labels.get(id) ?? id } : null)
  const rows = model.view.connections.map((r) => {
    const f = d.edits[r.id]
    if (!f) return { ...r, from_equipment: r.from_equipment && ref(r.from_equipment.id), to_equipment: r.to_equipment && ref(r.to_equipment.id) }
    return { ...r, ...(f.label ? { label: f.label.value ?? '' } : {}),
      ...(f.type ? { type: term(f.type) as TermRef | null } : {}), ...(f.medium ? { medium: term(f.medium) as TermRef | null } : {}),
      from_equipment: f.from_equipment ? ref(f.from_equipment.value) : r.from_equipment && ref(r.from_equipment.id),
      to_equipment: f.to_equipment ? ref(f.to_equipment.value) : r.to_equipment && ref(r.to_equipment.id) }
  })
  return [...rows, ...d.connections.map((c): ConnectionRow => ({
    kind: 'connection', id: c.key, iri: '', type: c.type, medium: c.medium, directed: true, locked: [], evidence: [],
    label: c.label || `${labels.get(c.from) ?? c.from} → ${labels.get(c.to) ?? c.to}`,
    from_equipment: ref(c.from), to_equipment: ref(c.to), from_point: null, to_point: null,
  }))]
}

/** Draft objects and existing rows the draft changes. */
export function draftTouched(d: Draft): Set<string> {
  return new Set([...d.equipment.map((e) => e.key), ...d.connections.map((c) => c.key), ...Object.keys(d.edits)])
}

// ------------------------------------------------------------ connection points

export interface PortAssignment { from: ConnectionPointRow | null; to: ConnectionPointRow | null }

const WANT = { from: 'outlet', to: 'inlet' } as const

function freePorts(model: ModelResponse, equipment: string, end: 'from' | 'to', medium: DraftTerm | null, claimed: Set<string>) {
  return model.view.connection_points.filter((p) => p.equipment?.id === equipment && !p.connection && !claimed.has(p.id)
    && (p.direction === WANT[end] || p.direction === 'bidirectional')
    && (!medium || !p.medium?.iri || p.medium.iri === medium.iri))
}

/** The existing connection point each new connection's ends join, or null where one is minted
 * (223P/WaTr only; Brick has no connection points). Ends that name a point claim it first; an
 * ``auto`` end then takes its equipment's free point of the right direction and a compatible
 * medium when there is exactly one, so ports are reused where that is unambiguous and minted
 * otherwise. */
export function assignPorts(model: ModelResponse, d: Draft): Map<string, PortAssignment> {
  const out = new Map<string, PortAssignment>(d.connections.map((c) => [c.key, { from: null, to: null }]))
  if (model.info.family !== 's223') return out
  const byId = new Map(model.view.connection_points.map((p) => [p.id, p]))
  const claimed = new Set<string>()
  for (const c of d.connections) {
    for (const end of ['from', 'to'] as const) {
      const choice = c[`${end}_point`]
      const port = choice === 'auto' || choice === 'new' ? undefined : byId.get(choice)
      if (port) { out.get(c.key)![end] = port; claimed.add(port.id) }
    }
  }
  for (const c of d.connections) {
    for (const end of ['from', 'to'] as const) {
      if (c[`${end}_point`] !== 'auto' || isDraftId(c[end])) continue
      const free = freePorts(model, c[end], end, c.medium, claimed)
      if (free.length === 1) { out.get(c.key)![end] = free[0]; claimed.add(free[0].id) }
    }
  }
  return out
}

/** Connection points a new connection's end could join: free, the right direction, not taken by another drafted connection. */
export function portOptions(model: ModelResponse, d: Draft, key: string, end: 'from' | 'to'): ConnectionPointRow[] {
  const c = d.connections.find((x) => x.key === key)
  if (!c || model.info.family !== 's223' || isDraftId(c[end])) return []
  const taken = new Set<string>()
  for (const [k, a] of assignPorts(model, d)) if (k !== key) for (const p of [a.from, a.to]) if (p) taken.add(p.id)
  return freePorts(model, c[end], end, null, taken)
}

// ------------------------------------------------------------------ compile

/** The draft as one ordered list of operations (new equipment, then new connections, then edits,
 * so placeholders are defined before they are used) and what must be settled before it can be saved. */
export function compileDraft(model: ModelResponse, d: Draft): { ops: Record<string, unknown>[]; problems: DraftProblem[] } {
  const s223 = model.info.family === 's223'
  const hasProcess = model.info.profile === 'watr'
  const ops: Record<string, unknown>[] = []
  const problems: DraftProblem[] = []
  const equipment = new Set([...model.view.equipment.map((e) => e.id), ...d.equipment.map((e) => e.key)])

  for (const e of d.equipment) {
    if (!e.label.trim()) problems.push({ id: e.key, message: 'needs a name' })
    if (!e.type) problems.push({ id: e.key, message: 'needs an equipment type' })
    ops.push({ op: 'create_equipment', id: e.key, label: e.label.trim(), type: e.type?.iri,
      ...(hasProcess && e.process ? { process: e.process.iri } : {}) })
  }

  const ports = assignPorts(model, d)
  for (const c of d.connections) {
    const p = ports.get(c.key)!
    if (c.from === c.to) problems.push({ id: c.key, message: 'connects equipment to itself' })
    if (!equipment.has(c.from) || !equipment.has(c.to)) problems.push({ id: c.key, message: 'an end is no longer in the model' })
    if (s223 && !c.medium && !p.from?.medium?.iri && !p.to?.medium?.iri) {
      problems.push({ id: c.key, message: 'needs a medium (what the pipe or duct carries)' })
    }
    ops.push({ op: 'create_connection', id: c.key, from_equipment: c.from, to_equipment: c.to,
      ...(c.label.trim() ? { label: c.label.trim() } : {}),
      ...(s223 && c.medium ? { medium: c.medium.iri } : {}),
      ...(s223 && c.type ? { type: c.type.iri } : {}),
      ...(p.from ? { from_point: p.from.id } : {}),
      ...(p.to ? { to_point: p.to.id } : {}) })
  }

  for (const [id, fields] of Object.entries(d.edits)) {
    const kind = model.view.equipment.some((e) => e.id === id) ? 'equipment'
      : model.view.connections.some((c) => c.id === id) ? 'connection' : null
    if (!kind) { problems.push({ id, message: 'is no longer in the model' }); continue }
    const op: Record<string, unknown> = { op: `update_${kind}`, id }
    for (const [f, v] of Object.entries(fields)) {
      if (!v) continue
      if (f === 'label' && !(v.value ?? '').trim()) problems.push({ id, message: 'name cannot be empty' })
      op[f] = f === 'label' ? (v.value ?? '').trim() : v.value
    }
    if (kind === 'connection') {
      const cx = model.view.connections.find((c) => c.id === id)!
      const from = fields.from_equipment?.value ?? cx.from_equipment?.id
      const to = fields.to_equipment?.value ?? cx.to_equipment?.id
      if (from && from === to) problems.push({ id, message: 'connects equipment to itself' })
    }
    ops.push(op)
  }
  return { ops, problems }
}
