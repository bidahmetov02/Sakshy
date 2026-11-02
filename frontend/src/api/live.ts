import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { removeCheck, staleAfter, upsertCheck, type ListFilter } from '../lib/live'
import type { Check } from './client'
import { keys } from './queries'

export type LiveStatus = 'connecting' | 'live' | 'reconnecting'

/** A stream the browser gave up on (an HTTP error) is reopened after this long. */
const REOPEN_MS = 5000

function cachedCheck(qc: QueryClient, id: number): Check | undefined {
  const one = qc.getQueryData<Check>(keys.check(id))
  if (one) return one
  for (const [, list] of qc.getQueriesData<Check[]>({ queryKey: ['checks', 'list'] })) {
    const found = list?.find((c) => c.id === id)
    if (found) return found
  }
  return undefined
}

function applyCheck(qc: QueryClient, check: Check) {
  const stale = staleAfter(cachedCheck(qc, check.id), check)
  qc.setQueryData(keys.check(check.id), check)
  for (const [key, list] of qc.getQueriesData<Check[]>({ queryKey: ['checks', 'list'] })) {
    if (list) qc.setQueryData(key, upsertCheck(list, check, key[2] as ListFilter))
  }
  if (stale.history) {
    void qc.invalidateQueries({ queryKey: keys.results(check.id) })
    void qc.invalidateQueries({ queryKey: ['stats', check.id] })
  }
  if (stale.incidents) void qc.invalidateQueries({ queryKey: ['incidents'] })
  if (stale.groups) void qc.invalidateQueries({ queryKey: keys.groups })
  if (stale.maintenance) void qc.invalidateQueries({ queryKey: keys.maintenance })
}

function dropCheck(qc: QueryClient, id: number) {
  qc.removeQueries({ queryKey: keys.check(id) })
  for (const [key, list] of qc.getQueriesData<Check[]>({ queryKey: ['checks', 'list'] })) {
    if (list) qc.setQueryData(key, removeCheck(list, id))
  }
  void qc.invalidateQueries({ queryKey: keys.groups })
}

/**
 * Keeps the signed-in user's checks live over SSE (`design/sequences.md` (c)). On `ready`,
 * which also follows every reconnect, everything check-related is refetched, so missed
 * events don't matter.
 */
export function useLiveUpdates(): LiveStatus {
  const qc = useQueryClient()
  const [status, setStatus] = useState<LiveStatus>('connecting')

  useEffect(() => {
    let source: EventSource | null = null
    let reopen: ReturnType<typeof setTimeout> | undefined

    function open() {
      source = new EventSource('/api/events')
      source.addEventListener('ready', () => {
        setStatus('live')
        void qc.invalidateQueries({ queryKey: ['checks'] })
        void qc.invalidateQueries({ queryKey: keys.groups })
        void qc.invalidateQueries({ queryKey: ['incidents'] })
        void qc.invalidateQueries({ queryKey: keys.maintenance })
      })
      source.addEventListener('check', (e) => applyCheck(qc, JSON.parse(e.data) as Check))
      source.addEventListener('check_removed', (e) => dropCheck(qc, (JSON.parse(e.data) as { id: number }).id))
      source.onerror = () => {
        setStatus('reconnecting')
        if (source?.readyState !== EventSource.CLOSED) return // the browser retries by itself
        // An HTTP error: maybe the session ended (a 401 there signs the user out), maybe
        // the API is restarting. Ask, then try again.
        void qc.invalidateQueries({ queryKey: keys.me })
        reopen = setTimeout(open, REOPEN_MS)
      }
    }

    open()
    return () => {
      clearTimeout(reopen)
      source?.close()
    }
  }, [qc])

  return status
}
