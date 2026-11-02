import { describe, expect, it } from 'vitest'
import type { Check } from '../api/client'
import { describeError, displayState, sortChecks, stateSince, statusSentence, summarize } from './checks'

const NOW = Date.parse('2026-11-02T12:00:00Z')

let nextId = 1
function check(overrides: Partial<Check> = {}): Check {
  const id = nextId++
  return {
    id,
    name: `site ${id}`,
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

describe('displayState', () => {
  it('shows paused over the stored state', () => {
    expect(displayState({ state: 'pending', is_paused: true })).toBe('paused')
    expect(displayState({ state: 'down', is_paused: false })).toBe('down')
  })
})

describe('sortChecks', () => {
  it('puts the worst first, then sorts by name', () => {
    const list = [
      check({ name: 'b', state: 'up' }),
      check({ name: 'z', is_paused: true, state: 'pending' }),
      check({ name: 'a', state: 'up' }),
      check({ name: 'c', state: 'down' }),
      check({ name: 'd', state: 'failing' }),
      check({ name: 'e', state: 'pending' }),
    ]
    expect(sortChecks(list).map((c) => c.name)).toEqual(['c', 'd', 'e', 'a', 'b', 'z'])
    expect(list[0].name).toBe('b') // input untouched
  })
})

describe('summarize', () => {
  it('invites a first check when there are none', () => {
    expect(summarize([])).toMatchObject({ tone: 'neutral', headline: 'No sites watched yet.' })
  })

  it('says all up', () => {
    expect(summarize([check(), check()])).toEqual({ tone: 'up', headline: 'All 2 sites are up.', detail: null })
    expect(summarize([check()]).headline).toBe('Your site is up.')
  })

  it('counts down sites among the active ones', () => {
    const s = summarize([check({ state: 'down' }), check(), check(), check({ is_paused: true, state: 'pending' })])
    expect(s).toEqual({ tone: 'down', headline: '1 of 3 sites is down.', detail: '1 check paused.' })
    expect(summarize([check({ state: 'down' }), check({ state: 'down' }), check()]).headline).toBe(
      '2 of 3 sites are down.',
    )
    expect(summarize([check({ state: 'down' }), check({ state: 'down' })]).headline).toBe('All 2 sites are down.')
  })

  it('notes down sites whose alerts maintenance holds back, and failing ones', () => {
    const s = summarize([
      check({ state: 'down', in_maintenance: true }),
      check({ state: 'down' }),
      check({ state: 'failing' }),
    ])
    expect(s.detail).toBe('1 in maintenance, so alerts are held back. 1 more is failing.')
    expect(summarize([check({ state: 'down', in_maintenance: true })]).detail).toBe(
      'It is in maintenance, so alerts are held back.',
    )
  })

  it('reports failing as unconfirmed', () => {
    expect(summarize([check({ state: 'failing' }), check()])).toEqual({
      tone: 'failing',
      headline: '1 of 2 sites is failing.',
      detail: 'Re-checking before raising an alert.',
    })
  })

  it('handles pending and paused checks', () => {
    expect(summarize([check({ state: 'pending' })]).headline).toBe('Checking your site for the first time.')
    expect(summarize([check({ state: 'pending' }), check()])).toEqual({
      tone: 'up',
      headline: '1 site is up.',
      detail: '1 is waiting for a first check.',
    })
    expect(summarize([check({ is_paused: true }), check({ is_paused: true })]).headline).toBe(
      'All 2 checks are paused.',
    )
  })
})

describe('stateSince', () => {
  it('counts down time from the incident start, up time from the state change', () => {
    expect(stateSince(check({ state: 'down', incident_started_at: '2026-11-02T11:48:00Z' }), NOW)).toBe('Down 12 min')
    expect(stateSince(check(), NOW)).toBe('Up 4 h')
    expect(stateSince(check({ is_paused: true }), NOW)).toBe('Paused')
    expect(stateSince(check({ state: 'failing' }), NOW)).toBe('Failing, re-checking')
  })
})

describe('describeError', () => {
  it('names the failure and adds the message', () => {
    expect(describeError(null, null)).toBeNull()
    expect(describeError('connect', 'connection refused')).toBe('Connection failed: connection refused')
    expect(describeError('status_mismatch', 'expected 200, got 503', 503)).toBe('Got status 503')
    expect(describeError('status_mismatch', 'expected status 200, got 503')).toBe(
      'Unexpected status code: expected status 200, got 503',
    )
    expect(describeError('timeout', null)).toBe('Timed out')
  })
})

describe('statusSentence', () => {
  it('says how long and why', () => {
    const down = check({
      state: 'down',
      incident_started_at: '2026-11-02T11:48:00Z',
      last_error_kind: 'timeout',
      last_error_message: 'no response in 10 s',
    })
    expect(statusSentence(down, NOW)).toBe('Down for 12 min. Timed out: no response in 10 s.')
    expect(statusSentence(check(), NOW)).toBe('Up for 4 h. Last check took 84 ms, status 200.')
    expect(statusSentence(check({ is_paused: true }), NOW)).toBe('Paused. Nothing is checked until you resume it.')
  })
})
