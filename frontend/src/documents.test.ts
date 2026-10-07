import { describe, expect, it } from 'vitest'
import { parsePageSelection } from './documents'

describe('PDF page selection', () => {
  it('accepts ranges and individual pages, sorts and removes duplicates', () => {
    expect(parsePageSelection('5, 1-3, 2', 10)).toEqual([1, 2, 3, 5])
  })
  it('accepts more than eight pages when the build batches a whole PDF', () => {
    expect(parsePageSelection('1-12', 12, 12)).toEqual(Array.from({ length: 12 }, (_, i) => i + 1))
    expect(() => parsePageSelection('1-13', 12, 12)).toThrow()
  })
  it.each(['', '0', '11', '3-1', '1, hi', '1.5', '1-9', '1-8, 10', '1-99999999'])('rejects invalid or oversized selection %s', (value) => {
    expect(() => parsePageSelection(value, 10)).toThrow()
  })
})
