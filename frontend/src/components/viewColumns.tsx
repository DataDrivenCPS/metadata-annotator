/** View specs as table columns: data loading, cell values and editors (relation, label, type). */
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { api } from '../api'
import type { Column } from '../selection'
import { useStore } from '../store'
import type { ViewCellItem, ViewData, ViewRow } from '../types'
import { AdjacencyCellEditor, RelationCellEditor, TextEditor } from './cells'
import { TermPicker } from './TermPicker'

const UPDATE_OP: Record<string, string> = {
  entity: 'update_entity', space: 'update_space', equipment: 'update_equipment', point: 'update_point',
  connection: 'update_connection', connection_point: 'update_connection_point',
}
const TYPE_KIND: Record<string, string> = { entity: 'class', space: 'location', equipment: 'equipment' }

/** A view's data for the shown revision, refetched when the model changes. */
export function useView(vid: string | null) {
  const projectId = useStore((s) => s.projectId)!
  const head = useStore((s) => s.model?.revision.id)
  const viewing = useStore((s) => s.viewing)
  const [data, setData] = useState<ViewData | null>(null)
  useEffect(() => {
    if (!vid) return
    let alive = true
    api.view(projectId, vid, viewing ?? undefined).then((d) => alive && setData(d)).catch(() => alive && setData(null))
    return () => { alive = false }
  }, [projectId, vid, head, viewing])
  return vid ? data : null
}

export type RowLike = { id: string; locked: string[] }

const cellText = (items: ViewCellItem[] | undefined) => (items ?? []).map((i) => i.label).join(', ')

export function columnsFor(view: ViewData, prefix: string, byId: Map<string, ViewRow>) {
  return view.columns.map((c): Column<RowLike> => ({
    key: `${prefix}${c.key}`, header: c.label,
    field: c.editor === 'none' ? undefined : `${prefix}${c.key}`,
    value: (r) => cellText(byId.get(r.id)?.cells[c.key]),
  }))
}

export function cellEditor(view: ViewData, prefix: string, byId: Map<string, ViewRow>, edit: (ops: Record<string, unknown>[]) => Promise<boolean>) {
  return (r: RowLike, col: Column<RowLike>, done: () => void): ReactNode => {
    const column = view.columns.find((c) => `${prefix}${c.key}` === col.key)
    const row = byId.get(r.id)
    if (!column || !row) return null
    const items = row.cells[column.key] ?? []
    if (column.editor === 'relation') return <RelationCellEditor row={row} column={column} items={items} done={done} />
    if (column.editor === 'adjacency') return <AdjacencyCellEditor row={row} column={column} items={items} done={done} />
    const op = UPDATE_OP[row.kind]
    if (column.editor === 'label' && op) return <TextEditor initial={row.label} onCancel={done}
      onCommit={(label) => { done(); void edit([{ op, id: row.id, label }]) }} />
    if (column.editor === 'type' && op && TYPE_KIND[row.kind]) return <TermPicker kind={TYPE_KIND[row.kind]} allowClear={false}
      initial={items[0]?.label} onCancel={done} onPick={(type) => { done(); if (type) void edit([{ op, id: row.id, type }]) }} />
    return null
  }
}

/** Columns that view specs add to a typed table (``builtin``), with their cell editors. */
export function useExtraColumns<R extends RowLike>(builtin: string) {
  const views = useStore((s) => s.views)
  const spec = views.find((v) => v.builtin === builtin)
  const data = useView(spec?.id ?? null)
  const edit = useStore((s) => s.edit)
  return useMemo(() => {
    if (!data) return { columns: [] as Column<R>[], editor: null }
    const byId = new Map(data.rows.map((r) => [r.id, r]))
    const prefix = `x:${data.id}:`
    return {
      columns: columnsFor(data, prefix, byId) as unknown as Column<R>[],
      editor: (r: R, col: Column<R>, done: () => void) => col.key.startsWith(prefix)
        ? cellEditor(data, prefix, byId, edit)(r, col as unknown as Column<RowLike>, done) : undefined,
    }
  }, [data, edit])
}
