import { describe, expect, it } from 'vitest'
import { pickAllShown, pickRows } from './recordSelection'

const order = ['r1', 'r2', 'r3', 'r4', 'r5']
const selectable = new Set(['r1', 'r2', 'r4', 'r5'])  // r3 is already in the model

describe('picking rows to build', () => {
  it('toggles one row', () => {
    const one = pickRows(new Set(), 'r2', false, null, order, selectable)
    expect([...one]).toEqual(['r2'])
    expect([...pickRows(one, 'r2', false, 'r2', order, selectable)]).toEqual([])
  })

  it('shift-click picks the range, skipping rows already in the model', () => {
    const range = pickRows(new Set(['r1']), 'r5', true, 'r1', order, selectable)
    expect([...range].sort()).toEqual(['r1', 'r2', 'r4', 'r5'])
    // shift-clicking a picked row clears the range back to the anchor
    expect([...pickRows(range, 'r4', true, 'r1', order, selectable)]).toEqual(['r5'])
  })

  it('never picks a row already in the model', () => {
    expect([...pickRows(new Set(), 'r3', false, null, order, selectable)]).toEqual([])
  })

  it('the header picks every shown row that can be, then none', () => {
    const all = pickAllShown(new Set(), ['r2', 'r3', 'r4'], selectable)
    expect([...all].sort()).toEqual(['r2', 'r4'])
    expect([...pickAllShown(new Set([...all, 'r5']), ['r2', 'r3', 'r4'], selectable)]).toEqual(['r5'])
  })
})
