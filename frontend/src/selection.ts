// Selection and table logic. Pure functions, unit-tested in selection.test.ts.
// A selection only ever holds application-managed ids - never labels, row numbers or
// screen positions - so sorting, filtering or re-rendering cannot change what is selected.

import type { Row, Selection } from './types'

export interface Modifiers { ctrl: boolean; shift: boolean }

export interface ClickTarget {
  id: string
  field?: string | null // column clicked, when it names a model field
  relationship?: boolean // connections are relationships
}

/** Selection after clicking a row/cell/node/edge. ``displayOrder`` is the order as shown. */
export function applyClick(
  sel: Selection, target: ClickTarget, mods: Modifiers, displayOrder: string[], anchor: string | null,
): Selection {
  const key = target.relationship ? 'relationship_ids' : 'entity_ids'
  const other = target.relationship ? 'entity_ids' : 'relationship_ids'
  let mine: string[]
  let others: string[]
  if (mods.shift && anchor && displayOrder.includes(anchor) && displayOrder.includes(target.id)) {
    const a = displayOrder.indexOf(anchor)
    const b = displayOrder.indexOf(target.id)
    const range = displayOrder.slice(Math.min(a, b), Math.max(a, b) + 1)
    mine = mods.ctrl ? Array.from(new Set([...sel[key], ...range])) : range
    others = mods.ctrl ? sel[other] : []
  } else if (mods.ctrl) {
    mine = sel[key].includes(target.id) ? sel[key].filter((i) => i !== target.id) : [...sel[key], target.id]
    others = sel[other]
  } else {
    mine = [target.id]
    others = []
  }
  let field_ids = sel.field_ids
  if (target.field !== undefined) {
    if (!mods.ctrl && !mods.shift) field_ids = target.field ? [target.field] : []
    else if (target.field && !field_ids.includes(target.field)) field_ids = [...field_ids, target.field]
  }
  const next = { ...sel, [key]: mine, [other]: others, field_ids } as Selection
  if (next.entity_ids.length + next.relationship_ids.length === 0) next.field_ids = []
  return next
}

/** Drop ids that no longer exist (e.g. after undo or a deletion). */
export function pruneSelection(sel: Selection, live: Set<string>): Selection {
  const entity_ids = sel.entity_ids.filter((i) => live.has(i))
  const relationship_ids = sel.relationship_ids.filter((i) => live.has(i))
  if (entity_ids.length === sel.entity_ids.length && relationship_ids.length === sel.relationship_ids.length) return sel
  return { ...sel, entity_ids, relationship_ids, field_ids: entity_ids.length + relationship_ids.length ? sel.field_ids : [] }
}

export function isSelected(sel: Selection, id: string) {
  return sel.entity_ids.includes(id) || sel.relationship_ids.includes(id)
}

export const FIELD_PHRASES: Record<string, string> = {
  equipment: 'equipment assignments', unit: 'units', quantity_kind: 'measured quantities',
  point_kind: 'point kinds', sensor_type: 'sensor types', label: 'names', type: 'types',
  process: 'treatment processes', medium: 'media', from_equipment: 'upstream ends',
  to_equipment: 'downstream ends', contained_in: 'containers', substance: 'substances',
  direction: 'directions', paired_with: 'pairings', maps_to: 'container mappings',
  from_point: 'upstream connection points', to_point: 'downstream connection points',
}

const cap = (s: string) => s.slice(0, 1).toUpperCase() + s.slice(1)

/** "Equipment assignments for 2 points" - same wording as the backend's describe_selection. */
export function summarize(sel: Selection, rows: Map<string, Row>): string {
  const counts = new Map<string, number>()
  for (const id of [...sel.entity_ids, ...sel.relationship_ids]) {
    const r = rows.get(id)
    if (r) counts.set(r.kind, (counts.get(r.kind) ?? 0) + 1)
  }
  const regions = sel.source_regions.length
  const regionText = regions ? `${regions} source region${regions === 1 ? '' : 's'}` : ''
  if (counts.size === 0) return regions ? cap(regionText) : 'Nothing selected (whole model)'
  const noun = [...counts].map(([k, n]) => `${n} ${k.replace(/_/g, ' ')}${n === 1 ? '' : 's'}`).join(', ')
  const withRegions = (t: string) => (regions ? `${t} + ${regionText}` : t)
  if (sel.field_ids.length) {
    const phrases = sel.field_ids.map((f) => FIELD_PHRASES[f] ?? f.replace(/_/g, ' '))
    return withRegions(`${cap(phrases.join(', '))} for ${noun}`)
  }
  return withRegions(cap(noun))
}

// ------------------------------------------------------------------- tables

export interface Column<R> {
  key: string
  header: string
  field?: string // model field this column shows (enables field selection + editing)
  value: (r: R) => string
  width?: number
}

export type SortState = { key: string; dir: 1 | -1 } | null

export function filterRows<R>(rows: R[], columns: Column<R>[], text: string): R[] {
  const q = text.trim().toLowerCase()
  if (!q) return rows
  const terms = q.split(/\s+/)
  return rows.filter((r) => {
    const hay = columns.map((c) => c.value(r)).join(' ').toLowerCase()
    return terms.every((t) => hay.includes(t))
  })
}

export function sortRows<R extends { id: string }>(rows: R[], columns: Column<R>[], sort: SortState): R[] {
  if (!sort) return rows
  const col = columns.find((c) => c.key === sort.key)
  if (!col) return rows
  return [...rows].sort((a, b) => {
    const cmp = col.value(a).localeCompare(col.value(b), undefined, { numeric: true, sensitivity: 'base' })
    return cmp !== 0 ? cmp * sort.dir : a.id.localeCompare(b.id) // stable, id-based tiebreak
  })
}
