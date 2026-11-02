/** Pure geometry for the response-time chart and uptime strip. No DOM. */
import type { Bucket } from '../api/client'

export interface Slot {
  /** Slot start and end, epoch ms. */
  start: number
  end: number
  total: number | null
  ok: number | null
  avg: number | null
  max: number | null
}

const FIVE_MIN = 5 * 60_000

/** Buckets are evenly spaced; the slot length is the gap between the first two. */
export function toSlots(buckets: readonly Bucket[]): Slot[] {
  const starts = buckets.map((b) => Date.parse(b.t))
  const step = starts.length > 1 ? starts[1] - starts[0] : FIVE_MIN
  return buckets.map((b, i) => ({
    start: starts[i],
    end: starts[i] + step,
    total: b.total ?? null,
    ok: b.ok ?? null,
    avg: b.avg_ms ?? null,
    max: b.max_ms ?? null,
  }))
}

/** A 0-based axis with round steps (1, 2, 2.5 or 5 × 10ⁿ, whole ms only) that covers `value`. */
export function niceScale(value: number, maxTicks = 4): { max: number; ticks: number[] } {
  const target = value > 0 ? value : 100
  const rough = target / maxTicks
  const magnitude = 10 ** Math.floor(Math.log10(rough))
  const step =
    [1, 2, 2.5, 5, 10]
      .map((m) => m * magnitude)
      .find((s) => s >= rough && s >= 1 && Number.isInteger(s)) ?? Math.max(1, 10 * magnitude)
  const max = Math.ceil(target / step) * step
  const ticks: number[] = []
  for (let v = 0; v <= max + step / 2; v += step) ticks.push(Math.round(v * 1000) / 1000)
  return { max, ticks }
}

export type Point = [x: number, y: number]

/** Splits a series at its gaps (null values): each run of present values is one line. */
export function segments(
  slots: readonly Slot[],
  x: (index: number) => number,
  y: (value: number) => number,
): Point[][] {
  const out: Point[][] = []
  let current: Point[] = []
  slots.forEach((slot, i) => {
    if (slot.avg == null) {
      if (current.length) out.push(current)
      current = []
    } else {
      current.push([x(i), y(slot.avg)])
    }
  })
  if (current.length) out.push(current)
  return out
}

export function linePath(points: readonly Point[]): string {
  return points.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join('')
}

/** The line closed down to the baseline: the area wash under it. */
export function areaPath(points: readonly Point[], baseline: number): string {
  if (!points.length) return ''
  const first = points[0]
  const last = points[points.length - 1]
  return `${linePath(points)}L${last[0].toFixed(1)},${baseline}L${first[0].toFixed(1)},${baseline}Z`
}

export type SlotStatus = 'none' | 'ok' | 'partial' | 'fail'

export function slotStatus(slot: Pick<Slot, 'total' | 'ok'>): SlotStatus {
  if (!slot.total) return 'none'
  if (slot.ok === slot.total) return 'ok'
  return slot.ok ? 'partial' : 'fail'
}

export interface Run {
  status: SlotStatus
  /** Index of the first slot and one past the last. */
  from: number
  to: number
}

/** Consecutive slots with the same status, merged: the strip draws one block per run. */
export function statusRuns(slots: readonly Slot[]): Run[] {
  const runs: Run[] = []
  slots.forEach((slot, i) => {
    const status = slotStatus(slot)
    const last = runs[runs.length - 1]
    if (last && last.status === status) last.to = i + 1
    else runs.push({ status, from: i, to: i + 1 })
  })
  return runs
}

const HOUR = 3_600_000
const STEPS_H = [1, 2, 3, 4, 6, 12, 24, 48, 72, 168]

/**
 * Tick times on local clock boundaries (whole hours or local midnights) inside
 * [start, end], at the smallest step that keeps the count at or below `maxTicks`.
 */
export function timeTicks(start: number, end: number, maxTicks: number): { ticks: number[]; daily: boolean } {
  const span = end - start
  const stepH = STEPS_H.find((h) => span / (h * HOUR) <= maxTicks) ?? STEPS_H[STEPS_H.length - 1]
  const daily = stepH >= 24
  const ticks: number[] = []
  const d = new Date(start)
  d.setMinutes(0, 0, 0)
  if (daily) {
    d.setHours(0)
    const days = stepH / 24
    while (d.getTime() < start) d.setDate(d.getDate() + 1)
    while (d.getTime() <= end) {
      ticks.push(d.getTime())
      d.setDate(d.getDate() + days)
    }
  } else {
    while (d.getTime() < start || d.getHours() % stepH) d.setHours(d.getHours() + 1)
    while (d.getTime() <= end) {
      ticks.push(d.getTime())
      d.setHours(d.getHours() + stepH)
    }
  }
  return { ticks, daily }
}

/** The slot under a pointer at `px`, given the plot width. */
export function slotAt(px: number, width: number, count: number): number {
  if (count <= 0 || width <= 0) return -1
  return Math.min(count - 1, Math.max(0, Math.floor((px / width) * count)))
}

/** The range's mean response time: each slot's average weighted by its run count. */
export function averageMs(slots: readonly Pick<Slot, 'avg' | 'total'>[]): number | null {
  let sum = 0
  let count = 0
  for (const s of slots) {
    if (s.avg == null || !s.total) continue
    sum += s.avg * s.total
    count += s.total
  }
  return count ? Math.round(sum / count) : null
}
