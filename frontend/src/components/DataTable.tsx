import { useMemo, useState, type ReactNode } from 'react'
import { isActive, issuesOnSelection } from '../assistant'
import { filterRows, isSelected, sortRows, type Column, type SortState } from '../selection'
import { useStore } from '../store'

interface Props<R extends { id: string; locked: string[] }> {
  rows: R[]
  columns: Column<R>[]
  relationship?: boolean
  issueCounts: Map<string, number>
  highlighted: Set<string>
  editor: (row: R, col: Column<R>, done: () => void) => ReactNode | null
  toolbar?: ReactNode
  empty: string
}

export function DataTable<R extends { id: string; locked: string[] }>(p: Props<R>) {
  const selection = useStore((s) => s.selection)
  const click = useStore((s) => s.click)
  const issues = useStore((s) => s.model?.issues)
  const startAutofix = useStore((s) => s.startAutofix)
  const autofixing = useStore((s) => !!s.autofix)
  const viewing = useStore((s) => s.viewing)
  const running = useStore((s) => Object.values(s.runs).some(isActive))
  const [filter, setFilter] = useState('')
  const [sort, setSort] = useState<SortState>(null)
  const [editing, setEditing] = useState<{ id: string; key: string } | null>(null)

  const shown = useMemo(() => sortRows(filterRows(p.rows, p.columns, filter), p.columns, sort), [p.rows, p.columns, filter, sort])
  const order = shown.map((r) => r.id)
  const selectedHere = p.rows.filter((r) => isSelected(selection, r.id)).length

  const onCell = (e: React.MouseEvent, row: R, col: Column<R> | null) => {
    click({ id: row.id, field: col ? (col.field ?? null) : undefined, relationship: p.relationship },
          { ctrl: e.ctrlKey || e.metaKey, shift: e.shiftKey }, order)
  }

  return (
    <div className="table-wrap">
      <div className="table-toolbar">
        <input className="filter" placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <span className="muted">
          {shown.length} of {p.rows.length}{selectedHere ? ` · ${selectedHere} selected` : ''}
          {filter && selectedHere > shown.filter((r) => isSelected(selection, r.id)).length ? ' (some hidden by filter)' : ''}
        </span>
        {(() => {
          const fixable = selectedHere && !autofixing && !viewing ? issuesOnSelection(issues ?? [], selection) : []
          return fixable.length > 0 && <button className="link" disabled={running}
            title={running ? 'Wait for the assistant to finish'
              : 'The assistant works through the selection’s issues one at a time; you approve, skip or answer each proposal'}
            onClick={() => void startAutofix(fixable)}>Auto-fix {fixable.length} issue{fixable.length === 1 ? '' : 's'}</button>
        })()}
        <span className="spacer" />
        {(viewing ? null : p.toolbar)}
      </div>
      <div className="table-scroll">
        <table className="data">
          <thead>
            <tr>
              <th className="check" />
              {p.columns.map((c) => (
                <th key={c.key} style={{ width: c.width }} onClick={() => setSort(
                  sort?.key !== c.key ? { key: c.key, dir: 1 } : sort.dir === 1 ? { key: c.key, dir: -1 } : null)}>
                  {c.header}{sort?.key === c.key ? (sort.dir === 1 ? ' ▲' : ' ▼') : ''}
                </th>
              ))}
              <th className="issues-col" title="Open issues">!</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((row) => {
              const sel = isSelected(selection, row.id)
              return (
                <tr key={row.id} className={`${sel ? 'selected' : ''} ${p.highlighted.has(row.id) ? 'proposed' : ''}`}>
                  <td className="check" onClick={(e) => { e.stopPropagation()
                    click({ id: row.id, relationship: p.relationship }, { ctrl: true, shift: e.shiftKey }, order) }}>
                    <input type="checkbox" readOnly checked={sel} />
                  </td>
                  {p.columns.map((c) => {
                    const isEditing = editing?.id === row.id && editing.key === c.key
                    const fieldSel = sel && c.field && selection.field_ids.includes(c.field)
                    const locked = c.field && row.locked.includes(c.field)
                    return (
                      <td key={c.key}
                        className={`${fieldSel ? 'field-selected' : ''} ${c.field ? 'editable' : ''}`}
                        onClick={(e) => onCell(e, row, c)}
                        onDoubleClick={() => c.field && !viewing && setEditing({ id: row.id, key: c.key })}
                        title={c.field ? 'Click to select · double-click to edit' : undefined}>
                        {isEditing ? p.editor(row, c, () => setEditing(null)) : (
                          <>
                            {c.value(row) || <span className="muted">—</span>}
                            {locked && <span className="locked-dot" title="Set or confirmed by a person" />}
                          </>
                        )}
                      </td>
                    )
                  })}
                  <td className="issues-col">
                    {(p.issueCounts.get(row.id) ?? 0) > 0 && <span className="badge warn">{p.issueCounts.get(row.id)}</span>}
                  </td>
                </tr>
              )
            })}
            {shown.length === 0 && (
              <tr><td colSpan={p.columns.length + 2} className="empty">{p.rows.length ? 'No rows match the filter' : p.empty}</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
