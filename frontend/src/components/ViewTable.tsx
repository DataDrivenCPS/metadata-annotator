import { useMemo } from 'react'
import { useStore } from '../store'
import { DataTable } from './DataTable'
import { cellEditor, columnsFor, useView } from './viewColumns'

/** A table of its own, from a view spec. */
export function ViewTable({ vid }: { vid: string }) {
  const data = useView(vid)
  const edit = useStore((s) => s.edit)
  const model = useStore((s) => s.model)!
  const byId = useMemo(() => new Map((data?.rows ?? []).map((r) => [r.id, r])), [data])
  const issueCounts = useMemo(() => {
    const m = new Map<string, number>()
    for (const i of model.issues) if (i.resolution_state === 'open' && i.severity !== 'suggestion')
      for (const id of i.affected_ids) m.set(id, (m.get(id) ?? 0) + 1)
    return m
  }, [model])
  if (!data) return <p className="muted pad">Loading…</p>
  const rows = data.rows.map((r) => ({ id: r.id, locked: [] as string[] }))
  return <DataTable rows={rows} columns={columnsFor(data, '', byId)} issueCounts={issueCounts} highlighted={new Set()}
    editor={cellEditor(data, '', byId, edit)}
    empty={`Nothing here yet. Add ${data.label.toLowerCase()} with the assistant, or create one from the Relationships panel of a related object.`} />
}

