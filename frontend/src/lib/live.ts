/** How an SSE `check` event changes the cached check lists. Pure, so it can be tested. */
import type { Check } from '../api/client'

/** A list's filter, as its query key holds it: a group id, or 'all'. */
export type ListFilter = number | 'all'

function belongs(check: Check, filter: ListFilter): boolean {
  return filter === 'all' || check.group_id === filter
}

/** The list with the check put in, replaced, or taken out if it no longer belongs. */
export function upsertCheck(list: readonly Check[], check: Check, filter: ListFilter): Check[] {
  const rest = list.filter((c) => c.id !== check.id)
  if (!belongs(check, filter)) return rest.length === list.length ? (list as Check[]) : rest
  const index = list.findIndex((c) => c.id === check.id)
  if (index === -1) return [...list, check]
  const next = [...list]
  next[index] = check
  return next
}

export function removeCheck(list: readonly Check[], id: number): Check[] {
  return list.some((c) => c.id === id) ? list.filter((c) => c.id !== id) : (list as Check[])
}

export interface Stale {
  /** Results and charts: a run landed. */
  history: boolean
  /** Incident lists: one opened or closed. */
  incidents: boolean
  /** Group status and counts. */
  groups: boolean
  /** Maintenance lists: a window started or ended. */
  maintenance: boolean
}

/** What else on screen a change to a check makes out of date. Unknown before: everything. */
export function staleAfter(before: Check | undefined, after: Check): Stale {
  if (!before) return { history: true, incidents: true, groups: true, maintenance: true }
  return {
    history: before.last_checked_at !== after.last_checked_at,
    incidents: before.incident_started_at !== after.incident_started_at || before.state !== after.state,
    groups:
      before.state !== after.state ||
      before.is_paused !== after.is_paused ||
      before.in_maintenance !== after.in_maintenance ||
      before.group_id !== after.group_id,
    maintenance: before.in_maintenance !== after.in_maintenance,
  }
}
