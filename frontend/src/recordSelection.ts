// Picking CSV rows to build: click toggles one row, shift-click sets the range from the last row
// clicked to this one (to the state the clicked row takes). Only rows not yet in the model can be
// picked. Pure, so it can be tested on its own.
import { displayRange } from './selection'

/** The picked rows after clicking ``id``; ``order`` is the rows as shown, ``selectable`` those that can be picked. */
export function pickRows(picked: ReadonlySet<string>, id: string, shift: boolean, anchor: string | null,
                         order: readonly string[], selectable: ReadonlySet<string>): Set<string> {
  const next = new Set(picked)
  const on = !picked.has(id)
  for (const r of (shift && displayRange(order, anchor, id)) || [id]) {
    if (!selectable.has(r)) continue
    if (on) next.add(r)
    else next.delete(r)
  }
  return next
}

/** Whether every / some shown row that can be picked is picked (the header checkbox's state). */
export function shownPicked(picked: ReadonlySet<string>, shown: readonly string[], selectable: ReadonlySet<string>) {
  const candidates = shown.filter((id) => selectable.has(id))
  return { candidates, all: candidates.length > 0 && candidates.every((id) => picked.has(id)),
           some: candidates.some((id) => picked.has(id)) }
}

/** The header checkbox: pick every shown row that can be picked, or, when all of them are, none of them. */
export function pickAllShown(picked: ReadonlySet<string>, shown: readonly string[], selectable: ReadonlySet<string>): Set<string> {
  const { candidates, all } = shownPicked(picked, shown, selectable)
  const next = new Set(picked)
  for (const id of candidates) {
    if (all) next.delete(id)
    else next.add(id)
  }
  return next
}
