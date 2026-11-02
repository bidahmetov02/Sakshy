/** The status page editor's item list, and the public page's wording. Pure, so it can be tested. */
import type { Bucket, GroupStatus, PublicItem, PublicPage, StatusPage, StatusPageItemIn } from '../api/client'
import { formatDuration } from './format'

export interface DraftItem {
  check_id: number
  display_name: string
}

export function draftItems(page: Pick<StatusPage, 'items'>): DraftItem[] {
  return [...page.items]
    .sort((a, b) => a.position - b.position || a.id - b.id)
    .map((i) => ({ check_id: i.check_id, display_name: i.display_name }))
}

/** The item moved one place up (-1) or down (+1); unchanged at either end. */
export function moveItem(items: readonly DraftItem[], index: number, delta: -1 | 1): DraftItem[] {
  const to = index + delta
  if (index < 0 || to < 0 || index >= items.length || to >= items.length) return [...items]
  const next = [...items]
  ;[next[index], next[to]] = [next[to], next[index]]
  return next
}

/** The PUT body: positions follow the list order. */
export function toItemsIn(items: readonly DraftItem[]): StatusPageItemIn[] {
  return items.map((item, position) => ({ ...item, display_name: item.display_name.trim(), position }))
}

export function sameItems(a: readonly DraftItem[], b: readonly DraftItem[]): boolean {
  return (
    a.length === b.length &&
    a.every((item, i) => item.check_id === b[i].check_id && item.display_name.trim() === b[i].display_name.trim())
  )
}

/** Keyed by check id: what's wrong with each item's public name. */
export function itemErrors(items: readonly DraftItem[]): Record<number, string> {
  const errors: Record<number, string> = {}
  for (const item of items) {
    const name = item.display_name.trim()
    if (!name) errors[item.check_id] = 'Give it a public name.'
    else if (name.length > 200) errors[item.check_id] = 'Keep the name under 200 characters.'
  }
  return errors
}

export function publicPath(slug: string): string {
  return `/status/${slug}`
}

// ---------- Public page ----------

export const OVERALL: Record<GroupStatus, string> = {
  operational: 'All systems operational.',
  partial_outage: 'Partial outage.',
  major_outage: 'Major outage.',
}

/** What a visitor reads for one item: "Operational", "Down for 12 min", "Under maintenance". */
export function publicStateText(item: Pick<PublicItem, 'state' | 'since'>, now: number): string {
  const since = formatDuration(now - Date.parse(item.since))
  switch (item.state) {
    case 'up':
      return 'Operational'
    case 'down':
      return `Down for ${since}`
    case 'maintenance':
      return 'Under maintenance'
    case 'paused':
      return 'Not monitored'
    case 'pending':
      return 'Checking'
  }
}

/** Items whose state differs between two versions of a public page (all, with no earlier one). */
export function changedItems(before: PublicPage | undefined, after: PublicPage): number[] {
  const was = new Map(before?.items.map((i) => [i.id, i.state]))
  return after.items.filter((i) => was.get(i.id) !== i.state).map((i) => i.id)
}

export interface Day {
  /** Local midnight, epoch ms. */
  start: number
  total: number
  ok: number
}

/**
 * Hourly buckets folded into local calendar days, the last `days` of them, oldest first.
 * A day with no runs has total 0.
 */
export function dailyUptime(buckets: readonly Bucket[], days = 30, now: number = Date.now()): Day[] {
  const byDay = new Map<number, Day>()
  const today = new Date(now)
  today.setHours(0, 0, 0, 0)
  const out: Day[] = []
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(today)
    d.setDate(today.getDate() - i)
    const day = { start: d.getTime(), total: 0, ok: 0 }
    byDay.set(day.start, day)
    out.push(day)
  }
  for (const b of buckets) {
    if (!b.total) continue
    const d = new Date(b.t)
    d.setHours(0, 0, 0, 0)
    const day = byDay.get(d.getTime())
    if (!day) continue
    day.total += b.total
    day.ok += b.ok ?? 0
  }
  return out
}
