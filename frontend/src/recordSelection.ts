// Picking CSV rows to build: click toggles one row, shift-click sets the range from the last row
// clicked to this one (to the state the clicked row takes). Only rows not yet in the model can be
// picked. Pure, so it can be tested on its own.

/** The picked rows after clicking ``id``; ``order`` is the rows as shown, ``selectable`` those that can be picked. */
export function pickRows(picked: ReadonlySet<string>, id: string, shift: boolean, anchor: string | null,
                         order: readonly string[], selectable: ReadonlySet<string>): Set<string> {
  const next = new Set(picked)
  const on = !picked.has(id)
  const from = anchor ? order.indexOf(anchor) : -1
  const to = order.indexOf(id)
  const range = shift && from >= 0 && to >= 0
    ? order.slice(Math.min(from, to), Math.max(from, to) + 1) : [id]
  for (const r of range) {
    if (!selectable.has(r)) continue
    if (on) next.add(r)
    else next.delete(r)
  }
  return next
}

/** The header checkbox: pick every shown row that can be picked, or, when all of them are, none of them. */
export function pickAllShown(picked: ReadonlySet<string>, shown: readonly string[], selectable: ReadonlySet<string>): Set<string> {
  const candidates = shown.filter((id) => selectable.has(id))
  const all = candidates.length > 0 && candidates.every((id) => picked.has(id))
  const next = new Set(picked)
  for (const id of candidates) {
    if (all) next.delete(id)
    else next.add(id)
  }
  return next
}
