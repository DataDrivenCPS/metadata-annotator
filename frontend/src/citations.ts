import type { Citation, Proposal } from './types'

export const EXCERPT_CHARS = 400
/** A span covering nearly all of the text is a page-level citation, not a passage to point at. */
const WHOLE_TEXT = 0.9

const collapse = (text: string) => text.replace(/\s+/g, ' ').trim()

/** Where a citation points, as a person reads it: "plant.pdf · page 3 · Pumps > Duty". */
export function citationLabel(citation: Citation): string {
  const { content, location } = citation
  const parts = [String(content.filename ?? content.name ?? 'Source')]
  if (location.page) parts.push(`page ${location.page}`)
  if (typeof location.row === 'number') parts.push(`row ${location.row + 1}`)
  if (typeof location.column === 'number') parts.push(`column ${location.column + 1}`)
  const section = Array.isArray(content.section) ? content.section.slice(-2).join(' > ') : ''
  if (section) parts.push(section)
  return parts.join(' · ')
}

/** A whole source (at a page, for a PDF) to open like a citation: a proposal's list of the files it read. */
export function sourceCitation(sourceId: string, filename: string, page?: number | null): Citation {
  return { id: '', source_id: sourceId, kind: 'source', status: 'unresolved', entity_ids: [],
    content: { filename }, location: { kind: 'document', page: page ?? null } }
}

/** A vision model's description of a page: useful, but not the source's own words. */
export const isGenerated = (citation: Citation) => citation.content.generated === true

/** The cited box in the source's own units: PDF points for a PDF page, pixels for an image. */
export function citationBox(citation: Citation): number[] | null {
  const box = citation.content.box_pt ?? citation.location.bbox
  return Array.isArray(box) && box.length === 4 && box.every((v) => typeof v === 'number') ? box as number[] : null
}

/** The smallest box around all of ``boxes``, to zoom to several at once. */
export function unionBox(boxes: number[][]): number[] | null {
  if (!boxes.length) return null
  const left = Math.min(...boxes.map((b) => b[0])), top = Math.min(...boxes.map((b) => b[1]))
  const right = Math.max(...boxes.map((b) => b[0] + b[2])), bottom = Math.max(...boxes.map((b) => b[1] + b[3]))
  return [left, top, right - left, bottom - top]
}

/** A box as percentages of its page, to place it over the page image at any zoom. */
export function boxPercent(box: number[], width: number, height: number) {
  return { left: `${box[0] / width * 100}%`, top: `${box[1] / height * 100}%`,
    width: `${box[2] / width * 100}%`, height: `${box[3] / height * 100}%` }
}

/** The pan and zoom that show a box in a frame, with room around it, never past the whole image. */
export function zoomToBox(box: number[], frameWidth: number, frameHeight: number, imageWidth: number, imageHeight: number) {
  const fit = Math.min(frameWidth / imageWidth, frameHeight / imageHeight)
  const scale = Math.max(fit, Math.min(frameWidth / (box[2] * 1.5), frameHeight / (box[3] * 1.5)))
  return { scale, x: frameWidth / 2 - (box[0] + box[2] / 2) * scale, y: frameHeight / 2 - (box[1] + box[3] / 2) * scale }
}

/** The span of a cited passage in the text now on screen, or null when it cannot be pointed at.
 * Stored offsets are used only if they still hold that passage; otherwise the passage is looked
 * for, so a highlight never lands on words the citation did not quote. */
export function locatePassage(text: string, citation: Citation): [number, number] | null {
  const passage = typeof citation.content.text === 'string' ? citation.content.text : ''
  if (!passage.trim() || isGenerated(citation)) return null
  const range = citation.location.text_range
  let span: [number, number] | null = null
  if (Array.isArray(range) && range.length === 2 && range[0] >= 0 && range[1] <= text.length && range[0] < range[1]
      && collapse(text.slice(range[0], range[1])) === collapse(passage)) {
    span = [range[0], range[1]]
  } else {
    const at = text.indexOf(passage)
    if (at >= 0) span = [at, at + passage.length]
  }
  if (!span || span[1] - span[0] >= text.length * WHOLE_TEXT) return null
  return span
}

/** The excerpt to show first: short enough to scan, with whether there is more to see. */
export function excerpt(text: string): { text: string; truncated: boolean } {
  if (text.length <= EXCERPT_CHARS) return { text, truncated: false }
  return { text: text.slice(0, EXCERPT_CHARS).trimEnd() + '…', truncated: true }
}

/** Records to list: the first ``limit``, plus the cited one wherever it falls. */
export function recordsToShow<T extends { id: string }>(records: T[], citedId: string | null, limit = 1000): T[] {
  const first = records.slice(0, limit)
  if (!citedId || first.some((r) => r.id === citedId)) return first
  const cited = records.find((r) => r.id === citedId)
  return cited ? [...first, cited] : first
}

/** "4 of 5 changes link to a source", from the proposal alone, before anything is fetched. "Link", not
 * "cite": a create operation the model cited nothing on is given all the sources it was shown. */
export function citationSummary(proposal: Proposal): { cited: number; total: number; read: number } {
  const citedIds = new Set<string>()
  const citedEntities = new Set<string>()
  for (const op of proposal.operations) {
    const evidence = Array.isArray(op.evidence) ? op.evidence as string[] : []
    evidence.forEach((id) => citedIds.add(id))
    if (evidence.length && typeof op.id === 'string') citedEntities.add(op.id)
  }
  const changes = proposal.changes.filter((c) => c.change !== 'deleted')
  const read = new Set(proposal.evidence.filter((e) => e.kind === 'observation' && !citedIds.has(e.ref)).map((e) => e.ref))
  return { cited: changes.filter((c) => citedEntities.has(c.entity_id)).length, total: changes.length, read: read.size }
}
