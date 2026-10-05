/** Small inline cell editors shared by the model tables and view tables. */
import { useState } from 'react'
import { useStore } from '../store'
import type { ViewCellItem, ViewColumn } from '../types'

export function TextEditor({ initial, onCommit, onCancel }: { initial: string; onCommit: (v: string) => void; onCancel: () => void }) {
  const [v, setV] = useState(initial)
  return (
    <input className="cell-input" autoFocus value={v} onChange={(e) => setV(e.target.value)}
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => { if (e.key === 'Enter' && v.trim()) onCommit(v.trim()); if (e.key === 'Escape') onCancel() }}
      onBlur={onCancel} />
  )
}

export function Select({ value, options, onCommit, onCancel }: {
  value: string; options: readonly (readonly [string, string])[]; onCommit: (v: string) => void; onCancel: () => void
}) {
  return (
    <select className="cell-input" autoFocus defaultValue={value} onClick={(e) => e.stopPropagation()}
      onChange={(e) => onCommit(e.target.value)} onBlur={onCancel}
      onKeyDown={(e) => e.key === 'Escape' && onCancel()}>
      {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
    </select>
  )
}

/** Chips for the current values (× removes the relationship) and a picker for adding one. */
export function RelationCellEditor({ row, column, items, done }: {
  row: { id: string }; column: ViewColumn; items: ViewCellItem[]; done: () => void
}) {
  const edit = useStore((s) => s.edit)
  const have = new Set(items.map((i) => i.id ?? i.iri))
  const options = (column.candidates ?? []).filter((c) => c.id !== row.id && !have.has(c.id ?? c.iri))
  const add = (c: ViewCellItem) => {
    const other = c.id ?? c.curie ?? c.iri!
    done()
    void edit([column.inverse
      ? { op: 'relate', subject: other, relation: column.relation, object: row.id }
      : { op: 'relate', subject: row.id, relation: column.relation, object: other }])
  }
  return (
    <span className="relation-cell" onClick={(e) => e.stopPropagation()}>
      {items.map((i) => <span key={i.relationship ?? i.label} className="chip">{i.label}
        {/* mouse-down, before the picker's blur closes the editor */}
        {i.relationship && <button className="link" title={`Remove (${column.relation_curie})`}
          onMouseDown={(e) => { e.preventDefault(); done(); void edit([{ op: 'unrelate', id: i.relationship }]) }}>×</button>}</span>)}
      <select autoFocus defaultValue="" onBlur={done} onKeyDown={(e) => e.key === 'Escape' && done()}
        onChange={(e) => { const c = options[Number(e.target.value)]; if (c) add(c) }}>
        <option value="" disabled>{options.length ? `add ${column.relation_label}…` : 'nothing else fits'}</option>
        {options.map((c, i) => <option key={i} value={i}>{c.label}{c.curie ? ` (${c.curie})` : ''}</option>)}
      </select>
    </span>
  )
}

/** Spaces next to this one (REC: sharing a wall): × removes, the picker adds. */
export function AdjacencyCellEditor({ row, column, items, done }: {
  row: { id: string }; column: ViewColumn; items: ViewCellItem[]; done: () => void
}) {
  const edit = useStore((s) => s.edit)
  const have = new Set(items.map((i) => i.id))
  const options = (column.candidates ?? []).filter((c) => c.id !== row.id && !have.has(c.id))
  return (
    <span className="relation-cell" onClick={(e) => e.stopPropagation()}>
      {items.map((i) => <span key={i.id ?? i.label} className="chip">{i.label}
        <button className="link" title="Not adjacent"
          onMouseDown={(e) => { e.preventDefault(); done(); void edit([{ op: 'unmake_adjacent', space: row.id, other: i.id }]) }}>×</button></span>)}
      <select autoFocus defaultValue="" onBlur={done} onKeyDown={(e) => e.key === 'Escape' && done()}
        onChange={(e) => {
          const c = options[Number(e.target.value)]
          if (c) { done(); void edit([{ op: 'make_adjacent', space: row.id, other: c.id }]) }
        }}>
        <option value="" disabled>{options.length ? 'add adjacent space…' : 'no other spaces'}</option>
        {options.map((c, i) => <option key={i} value={i}>{c.label}</option>)}
      </select>
    </span>
  )
}
