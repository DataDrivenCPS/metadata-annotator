/** Small inline cell editors shared by the model tables and view tables. */
import { useState } from 'react'
import { useStore } from '../store'
import type { EntityPropertySpec, RelationCandidate, ViewCellItem, ViewColumn } from '../types'

export function TextEditor({ initial, onCommit, onCancel }: { initial: string; onCommit: (v: string) => void; onCancel: () => void }) {
  const [v, setV] = useState(initial)
  return (
    <input className="cell-input" autoFocus value={v} onChange={(e) => setV(e.target.value)}
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => { if (e.key === 'Enter' && v.trim()) onCommit(v.trim()); if (e.key === 'Escape') onCancel() }}
      onBlur={onCancel} />
  )
}

/** A Brick entity property's value and unit: Enter saves, an empty value removes it. */
export function PropertyEditor({ spec, value, unit, onCommit, onCancel }: {
  spec: EntityPropertySpec; value: string; unit: string
  onCommit: (value: string, unit: string) => void; onCancel: () => void
}) {
  const [v, setV] = useState(value)
  const units = spec.unit_terms ?? []
  const [u, setU] = useState(unit || (units.length === 1 ? units[0].iri : ''))
  const done = () => onCommit(v.trim(), u)
  return (
    <span className="property-editor" onClick={(e) => e.stopPropagation()}
      onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) onCancel() }}>
      {spec.values?.length ? (
        <select className="cell-input" autoFocus value={v} onChange={(e) => setV(e.target.value)}>
          <option value="" disabled>choose…</option>
          {spec.values.map((x) => <option key={x} value={x}>{x}</option>)}
        </select>
      ) : (
        <input className="cell-input" autoFocus value={v} placeholder={spec.numeric ? 'number' : 'value'}
          onChange={(e) => setV(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') done(); if (e.key === 'Escape') onCancel() }} />
      )}
      {units.length > 0 && (
        <select className="cell-input" value={u} onChange={(e) => setU(e.target.value)}>
          {units.length > 1 && <option value="" disabled>unit…</option>}
          {units.map((x) => <option key={x.iri} value={x.iri}>{x.symbol || x.label}</option>)}
        </select>
      )}
      <button className="link" onMouseDown={(e) => e.preventDefault()} onClick={done}>save</button>
    </span>
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

/** Picker options for relation objects: what the vocabulary expects first, then anything else
 * (allowed; validation reports what does not fit). Option values index into ``candidates``. */
export function CandidateOptions({ candidates, describe }: {
  candidates: readonly (RelationCandidate | ViewCellItem)[]; describe: (c: RelationCandidate | ViewCellItem) => string
}) {
  const items = candidates.map((c, i) => ({ c, i }))
  const fit = items.filter(({ c }) => c.fits !== false)
  const other = items.filter(({ c }) => c.fits === false)
  const opts = (list: typeof items) => list.map(({ c, i }) => <option key={i} value={i}>{describe(c)}</option>)
  if (!other.length) return <>{opts(fit)}</>
  return <>
    {fit.length > 0 && <optgroup label="Fits the vocabulary">{opts(fit)}</optgroup>}
    <optgroup label="Anything else (validation will check)">{opts(other)}</optgroup>
  </>
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
        <option value="" disabled>{options.length ? `add ${column.relation_label}…` : 'nothing else to add'}</option>
        <CandidateOptions candidates={options} describe={(c) => `${c.label}${c.curie ? ` (${c.curie})` : ''}`} />
      </select>
    </span>
  )
}
