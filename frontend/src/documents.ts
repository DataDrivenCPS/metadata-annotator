export const MAX_BUILD_PAGES = 8

export function parsePageSelection(value: string, pageCount: number): number[] {
  const pages = new Set<number>()
  for (const part of value.split(',')) {
    const match = /^\s*(\d+)(?:\s*-\s*(\d+))?\s*$/.exec(part)
    if (!match) throw new Error('Enter page numbers or ranges, such as 1-3, 5.')
    const first = Number(match[1])
    const last = Number(match[2] ?? match[1])
    if (first < 1 || last < first || last > pageCount) throw new Error(`Choose pages between 1 and ${pageCount}.`)
    if (last - first + 1 > MAX_BUILD_PAGES) throw new Error(`Choose at most ${MAX_BUILD_PAGES} pages per build.`)
    for (let page = first; page <= last; page++) pages.add(page)
  }
  if (pages.size > MAX_BUILD_PAGES) throw new Error(`Choose at most ${MAX_BUILD_PAGES} pages per build.`)
  return [...pages].sort((a, b) => a - b)
}
