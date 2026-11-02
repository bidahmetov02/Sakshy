import { describe, expect, it } from 'vitest'
import type { Check } from '../api/client'
import { removeCheck, staleAfter, upsertCheck } from './live'

function check(id: number, overrides: Partial<Check> = {}): Check {
  return {
    id,
    name: `c${id}`,
    url: 'https://example.com',
    interval_s: 60,
    timeout_s: 10,
    expected_status: 200,
    expected_body: null,
    group_id: null,
    alert_emails: ['a@x.com'],
    effective_alert_emails: ['a@x.com'],
    is_paused: false,
    created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-01T00:00:00Z',
    state: 'up',
    state_changed_at: '2026-11-02T08:00:00Z',
    next_run_at: '2026-11-02T12:01:00Z',
    last_checked_at: '2026-11-02T11:59:00Z',
    last_ok: true,
    last_status_code: 200,
    last_response_ms: 84,
    last_error_kind: null,
    last_error_message: null,
    incident_started_at: null,
    in_maintenance: false,
    ...overrides,
  }
}

describe('upsertCheck', () => {
  it('replaces a check in place', () => {
    const list = [check(1), check(2), check(3)]
    const next = upsertCheck(list, check(2, { state: 'down' }), 'all')
    expect(next.map((c) => c.id)).toEqual([1, 2, 3])
    expect(next[1].state).toBe('down')
    expect(list[1].state).toBe('up') // input untouched
  })

  it('adds a new check to lists it belongs to', () => {
    expect(upsertCheck([check(1)], check(2), 'all').map((c) => c.id)).toEqual([1, 2])
    expect(upsertCheck([], check(2, { group_id: 7 }), 7).map((c) => c.id)).toEqual([2])
  })

  it('takes a check out of a group list it left, and keeps lists it never was in', () => {
    const groupList = [check(1, { group_id: 7 }), check(2, { group_id: 7 })]
    expect(upsertCheck(groupList, check(2, { group_id: null }), 7).map((c) => c.id)).toEqual([1])
    const other = [check(5, { group_id: 8 })]
    expect(upsertCheck(other, check(2, { group_id: 7 }), 8)).toBe(other)
  })
})

describe('removeCheck', () => {
  it('drops the check, and keeps the same list when it was absent', () => {
    const list = [check(1), check(2)]
    expect(removeCheck(list, 1).map((c) => c.id)).toEqual([2])
    expect(removeCheck(list, 9)).toBe(list)
  })
})

describe('staleAfter', () => {
  it('marks everything stale for a check not seen before', () => {
    expect(staleAfter(undefined, check(1))).toEqual({ history: true, incidents: true, groups: true, maintenance: true })
  })

  it('marks history stale after a run, and nothing else when the state held', () => {
    expect(staleAfter(check(1), check(1, { last_checked_at: '2026-11-02T12:00:00Z' }))).toEqual({
      history: true,
      incidents: false,
      groups: false,
      maintenance: false,
    })
  })

  it('marks incidents and groups stale when a check goes down', () => {
    const down = check(1, { state: 'down', incident_started_at: '2026-11-02T11:58:00Z' })
    expect(staleAfter(check(1), down)).toMatchObject({ incidents: true, groups: true })
  })

  it('marks groups and maintenance stale when a window starts', () => {
    expect(staleAfter(check(1), check(1, { in_maintenance: true }))).toMatchObject({
      groups: true,
      maintenance: true,
      history: false,
    })
  })
})
