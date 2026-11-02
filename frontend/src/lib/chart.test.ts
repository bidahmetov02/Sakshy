import { describe, expect, it } from 'vitest'
import type { Bucket } from '../api/client'
import {
  areaPath,
  averageMs,
  linePath,
  niceScale,
  segments,
  slotAt,
  slotStatus,
  statusRuns,
  timeTicks,
  toSlots,
} from './chart'

const T0 = Date.parse('2026-11-02T00:00:00Z') // tests run with TZ=UTC
const HOUR = 3_600_000

function buckets(values: (Partial<Bucket> | null)[], stepMs = 5 * 60_000): Bucket[] {
  return values.map((v, i) => ({ t: new Date(T0 + i * stepMs).toISOString(), ...(v ?? {}) }))
}

describe('toSlots', () => {
  it('derives the slot length from the spacing', () => {
    const slots = toSlots(buckets([{ total: 2, ok: 2, avg_ms: 10, max_ms: 20 }, null], HOUR))
    expect(slots[0]).toEqual({ start: T0, end: T0 + HOUR, total: 2, ok: 2, avg: 10, max: 20 })
    expect(slots[1]).toMatchObject({ total: null, avg: null })
  })

  it('falls back to 5 minutes with one bucket', () => {
    const [slot] = toSlots(buckets([null]))
    expect(slot.end - slot.start).toBe(5 * 60_000)
  })
})

describe('niceScale', () => {
  it.each([
    [0, 100, [0, 25, 50, 75, 100]],
    [87, 100, [0, 25, 50, 75, 100]],
    [130, 150, [0, 50, 100, 150]],
    [1234, 1500, [0, 500, 1000, 1500]],
    [9000, 10000, [0, 2500, 5000, 7500, 10000]],
    [9, 10, [0, 5, 10]], // never a 2.5 ms step: labels are whole ms
    [2, 2, [0, 1, 2]],
  ])('covers %i with max %i', (value, max, ticks) => {
    expect(niceScale(value)).toEqual({ max, ticks })
  })
})

describe('segments', () => {
  it('breaks the line at gaps', () => {
    const slots = toSlots(buckets([{ avg_ms: 1 }, { avg_ms: 2 }, null, { avg_ms: 3 }, null]))
    expect(segments(slots, (i) => i, (v) => v * 10)).toEqual([
      [
        [0, 10],
        [1, 20],
      ],
      [[3, 30]],
    ])
  })

  it('is empty with no data', () => {
    expect(segments(toSlots(buckets([null, null])), (i) => i, (v) => v)).toEqual([])
  })
})

describe('paths', () => {
  it('draws the line and closes the area to the baseline', () => {
    const points: [number, number][] = [
      [0, 5],
      [10, 2],
    ]
    expect(linePath(points)).toBe('M0.0,5.0L10.0,2.0')
    expect(areaPath(points, 20)).toBe('M0.0,5.0L10.0,2.0L10.0,20L0.0,20Z')
    expect(areaPath([], 20)).toBe('')
  })
})

describe('slot status and runs', () => {
  it('classifies each slot', () => {
    expect(slotStatus({ total: null, ok: null })).toBe('none')
    expect(slotStatus({ total: 0, ok: 0 })).toBe('none')
    expect(slotStatus({ total: 3, ok: 3 })).toBe('ok')
    expect(slotStatus({ total: 3, ok: 1 })).toBe('partial')
    expect(slotStatus({ total: 3, ok: 0 })).toBe('fail')
  })

  it('merges consecutive slots with the same status', () => {
    const slots = toSlots(
      buckets([null, { total: 1, ok: 1 }, { total: 1, ok: 1 }, { total: 2, ok: 0 }, { total: 1, ok: 1 }]),
    )
    expect(statusRuns(slots)).toEqual([
      { status: 'none', from: 0, to: 1 },
      { status: 'ok', from: 1, to: 3 },
      { status: 'fail', from: 3, to: 4 },
      { status: 'ok', from: 4, to: 5 },
    ])
  })
})

describe('averageMs', () => {
  it('weights each slot by its run count and skips gaps', () => {
    expect(averageMs([{ avg: 100, total: 3 }, { avg: 200, total: 1 }, { avg: null, total: null }])).toBe(125)
    expect(averageMs([{ avg: null, total: null }])).toBeNull()
  })
})

describe('timeTicks', () => {
  it('puts hourly ticks on whole local hours', () => {
    const start = T0 + 10 * 60_000 // 00:10
    const { ticks, daily } = timeTicks(start, start + 24 * HOUR, 8)
    expect(daily).toBe(false)
    expect(ticks.map((t) => new Date(t).getUTCHours())).toEqual([3, 6, 9, 12, 15, 18, 21, 0])
  })

  it('switches to local midnights for long spans', () => {
    const start = T0 + 5 * HOUR
    const { ticks, daily } = timeTicks(start, start + 7 * 24 * HOUR, 8)
    expect(daily).toBe(true)
    expect(ticks).toHaveLength(7)
    expect(ticks.every((t) => new Date(t).getUTCHours() === 0)).toBe(true)
  })

  it('thins month ticks to fit', () => {
    const { ticks } = timeTicks(T0, T0 + 30 * 24 * HOUR, 6)
    expect(ticks.length).toBeLessThanOrEqual(6)
  })
})

describe('slotAt', () => {
  it('maps a pointer to the slot under it, clamped', () => {
    expect(slotAt(0, 100, 10)).toBe(0)
    expect(slotAt(55, 100, 10)).toBe(5)
    expect(slotAt(100, 100, 10)).toBe(9)
    expect(slotAt(-4, 100, 10)).toBe(0)
    expect(slotAt(10, 100, 0)).toBe(-1)
  })
})
