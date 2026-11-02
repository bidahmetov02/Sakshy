import { describe, expect, it } from 'vitest'
import type { Bucket } from '../api/client'
import { changedItems, dailyUptime, draftItems, itemErrors, moveItem, publicStateText, sameItems, toItemsIn } from './statusPages'

const items = [
  { check_id: 1, display_name: 'API' },
  { check_id: 2, display_name: 'Web' },
  { check_id: 3, display_name: 'CDN' },
]

describe('item list', () => {
  it('reads saved items in position order, ties by id', () => {
    const page = {
      items: [
        { id: 9, check_id: 3, display_name: 'CDN', position: 1 },
        { id: 4, check_id: 2, display_name: 'Web', position: 0 },
        { id: 2, check_id: 1, display_name: 'API', position: 0 },
      ],
    }
    expect(draftItems(page).map((i) => i.check_id)).toEqual([1, 2, 3])
  })

  it('moves items within bounds', () => {
    expect(moveItem(items, 1, -1).map((i) => i.check_id)).toEqual([2, 1, 3])
    expect(moveItem(items, 1, 1).map((i) => i.check_id)).toEqual([1, 3, 2])
    expect(moveItem(items, 0, -1).map((i) => i.check_id)).toEqual([1, 2, 3])
    expect(moveItem(items, 2, 1).map((i) => i.check_id)).toEqual([1, 2, 3])
  })

  it('numbers positions in list order and trims names', () => {
    expect(toItemsIn([{ check_id: 2, display_name: ' Web ' }, items[0]])).toEqual([
      { check_id: 2, display_name: 'Web', position: 0 },
      { check_id: 1, display_name: 'API', position: 1 },
    ])
  })

  it('compares lists ignoring surrounding spaces', () => {
    expect(sameItems(items, items.map((i) => ({ ...i, display_name: ` ${i.display_name}` })))).toBe(true)
    expect(sameItems(items, moveItem(items, 0, 1))).toBe(false)
    expect(sameItems(items, items.slice(1))).toBe(false)
  })

  it('requires a public name', () => {
    expect(itemErrors([{ check_id: 1, display_name: '  ' }, items[1]])).toEqual({ 1: 'Give it a public name.' })
  })
})

describe('publicStateText', () => {
  const now = Date.parse('2026-11-02T12:00:00Z')
  it('describes each state for visitors', () => {
    expect(publicStateText({ state: 'up', since: '2026-11-01T00:00:00Z' }, now)).toBe('Operational')
    expect(publicStateText({ state: 'down', since: '2026-11-02T11:48:00Z' }, now)).toBe('Down for 12 min')
    expect(publicStateText({ state: 'maintenance', since: '2026-11-02T11:00:00Z' }, now)).toBe('Under maintenance')
    expect(publicStateText({ state: 'paused', since: '2026-11-02T11:00:00Z' }, now)).toBe('Not monitored')
  })
})

describe('dailyUptime', () => {
  // TZ=UTC: local days are UTC days.
  const now = Date.parse('2026-11-02T12:00:00Z')
  const hour = (iso: string, total: number | null, ok: number | null): Bucket => ({ t: iso, total, ok })

  it('folds hours into days, oldest first, ending today', () => {
    const days = dailyUptime(
      [
        hour('2026-11-01T10:00:00Z', 60, 60),
        hour('2026-11-01T11:00:00Z', 60, 30),
        hour('2026-11-02T09:00:00Z', 60, 60),
        hour('2026-11-02T10:00:00Z', null, null),
        hour('2026-09-01T10:00:00Z', 60, 0), // older than the window: ignored
      ],
      3,
      now,
    )
    expect(days).toEqual([
      { start: Date.parse('2026-10-31T00:00:00Z'), total: 0, ok: 0 },
      { start: Date.parse('2026-11-01T00:00:00Z'), total: 120, ok: 90 },
      { start: Date.parse('2026-11-02T00:00:00Z'), total: 60, ok: 60 },
    ])
  })

  it('returns 30 days by default', () => {
    expect(dailyUptime([], undefined, now)).toHaveLength(30)
  })
})

describe('changedItems', () => {
  const item = (id: number, state: 'up' | 'down') => ({ id, display_name: `i${id}`, state, since: '2026-11-02T00:00:00Z' })
  const page = (...items: ReturnType<typeof item>[]) => ({ title: 't', overall_status: 'operational' as const, items })

  it('lists items whose state moved, or every item with nothing before', () => {
    expect(changedItems(page(item(1, 'up'), item(2, 'up')), page(item(1, 'up'), item(2, 'down')))).toEqual([2])
    expect(changedItems(undefined, page(item(1, 'up'), item(2, 'up')))).toEqual([1, 2])
    expect(changedItems(page(item(1, 'up')), page(item(1, 'up'), item(3, 'up')))).toEqual([3])
  })
})
