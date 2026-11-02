import { describe, expect, it } from 'vitest'
import type { MaintenanceWindow } from '../api/client'
import {
  buildWindow,
  buildWindowPatch,
  editWindowValues,
  fromLocalInput,
  newWindowValues,
  toLocalInput,
  windowPhase,
  windowRange,
  windowTiming,
} from './maintenance'

// Tests run with TZ=UTC, so local inputs read as UTC.
const NOW = Date.parse('2026-11-02T14:20:00Z')
const MIN = 60_000

function win(overrides: Partial<MaintenanceWindow> = {}): MaintenanceWindow {
  return {
    id: 1,
    check_id: 5,
    group_id: null,
    starts_at: '2026-11-02T14:00:00Z',
    ends_at: '2026-11-02T16:00:00Z',
    reason: null,
    created_at: '2026-11-02T13:00:00Z',
    status: 'active',
    ...overrides,
  }
}

describe('local inputs', () => {
  it('round-trips a time', () => {
    expect(toLocalInput(NOW)).toBe('2026-11-02T14:20')
    expect(fromLocalInput('2026-11-02T14:20')).toBe(NOW)
    expect(fromLocalInput('')).toBeNaN()
  })

  it('suggests the next whole hour for a set start', () => {
    expect(newWindowValues(NOW, 'g:3')).toMatchObject({
      target: 'g:3',
      start: 'now',
      startsAt: '2026-11-02T15:00',
      duration: '60',
      endsAt: '2026-11-02T16:00',
    })
  })
})

describe('buildWindow', () => {
  const base = { ...newWindowValues(NOW), target: 'c:5' }

  it('starts now for the chosen duration', () => {
    expect(buildWindow({ ...base, duration: '30', reason: '  deploy ' }, NOW).body).toEqual({
      check_id: 5,
      starts_at: '2026-11-02T14:20:00.000Z',
      ends_at: '2026-11-02T14:50:00.000Z',
      reason: 'deploy',
    })
  })

  it('targets a group, at a set time, until a set time', () => {
    const { body } = buildWindow(
      { ...base, target: 'g:3', start: 'at', startsAt: '2026-11-03T01:00', duration: 'custom', endsAt: '2026-11-03T03:30' },
      NOW,
    )
    expect(body).toEqual({ group_id: 3, starts_at: '2026-11-03T01:00:00.000Z', ends_at: '2026-11-03T03:30:00.000Z' })
  })

  it('reports what is missing or out of order', () => {
    expect(buildWindow({ ...base, target: '' }, NOW).errors.target).toBeDefined()
    expect(buildWindow({ ...base, start: 'at', startsAt: '' }, NOW).errors.startsAt).toBeDefined()
    expect(buildWindow({ ...base, duration: 'custom', endsAt: '2026-11-02T14:00' }, NOW).errors.endsAt).toBe(
      'The end must be after the start.',
    )
    expect(
      buildWindow({ ...base, start: 'at', startsAt: '2026-11-02T10:00', duration: '60' }, NOW).errors.endsAt,
    ).toBe('The end must be in the future.')
    expect(buildWindow({ ...base, reason: 'x'.repeat(501) }, NOW).errors.reason).toBeDefined()
  })
})

describe('buildWindowPatch', () => {
  it('sends the start only while upcoming, and clears an empty reason', () => {
    const upcoming = win({ status: 'upcoming', starts_at: '2026-11-02T18:00:00Z', ends_at: '2026-11-02T19:00:00Z', reason: 'x' })
    const values = { ...editWindowValues(upcoming), endsAt: '2026-11-02T20:00', reason: ' ' }
    expect(buildWindowPatch(values, upcoming).body).toEqual({
      starts_at: '2026-11-02T18:00:00.000Z',
      ends_at: '2026-11-02T20:00:00.000Z',
      reason: null,
    })
    const active = win()
    expect(buildWindowPatch({ ...editWindowValues(active), reason: 'db' }, active).body).toEqual({
      ends_at: '2026-11-02T16:00:00.000Z',
      reason: 'db',
    })
  })

  it('rejects an end before the start', () => {
    const active = win()
    expect(buildWindowPatch({ ...editWindowValues(active), endsAt: '2026-11-02T13:00' }, active).errors.endsAt).toBeDefined()
  })
})

describe('wording', () => {
  const now = new Date(NOW)

  it('shows a range, with the end date only when it differs', () => {
    expect(windowRange(win(), now, 'en-GB')).toBe('Today 14:00–16:00')
    expect(windowRange(win({ ends_at: '2026-11-04T02:00:00Z' }), now, 'en-GB')).toBe('Today 14:00 to 4 Nov, 02:00')
  })

  it('places the window in time', () => {
    expect(windowPhase(win(), NOW)).toBe('active')
    expect(windowTiming(win(), NOW)).toBe('Ends in 1 h 40 min')
    const later = win({ starts_at: '2026-11-02T16:20:00Z', ends_at: '2026-11-02T17:00:00Z' })
    expect(windowTiming(later, NOW)).toBe('Starts in 2 h')
    const done = win({ starts_at: '2026-11-01T10:00:00Z', ends_at: '2026-11-02T14:00:00Z' })
    expect(windowPhase(done, NOW)).toBe('past')
    expect(windowTiming(done, NOW)).toBe('Ended 20 min ago')
    expect(windowPhase(win({ ends_at: new Date(NOW + MIN).toISOString() }), NOW)).toBe('active')
  })
})
