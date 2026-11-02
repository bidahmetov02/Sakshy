import { describe, expect, it } from 'vitest'
import { formatDuration, formatInterval, formatMs, formatUptime, formatWhen, plural } from './format'

describe('formatDuration', () => {
  it.each([
    [0, '0 s'],
    [-5000, '0 s'],
    [45_000, '45 s'],
    [60_000, '1 min'],
    [12 * 60_000 + 59_000, '12 min'],
    [3 * 3_600_000, '3 h'],
    [3 * 3_600_000 + 20 * 60_000, '3 h 20 min'],
    [4 * 86_400_000, '4 d'],
    [4 * 86_400_000 + 2 * 3_600_000 + 5 * 60_000, '4 d 2 h'],
  ])('%i ms reads %s', (ms, text) => {
    expect(formatDuration(ms)).toBe(text)
  })
})

describe('formatInterval', () => {
  it.each([
    [30, '30 s'],
    [60, '1 min'],
    [90, '1 min 30 s'],
    [3600, '1 h'],
    [3690, '1 h 1 min 30 s'],
  ])('%i s reads %s', (s, text) => {
    expect(formatInterval(s)).toBe(text)
  })
})

describe('formatMs', () => {
  it('handles missing, ms and seconds', () => {
    expect(formatMs(null)).toBe('–')
    expect(formatMs(undefined)).toBe('–')
    expect(formatMs(84)).toBe('84 ms')
    expect(formatMs(999.6)).toBe('1000 ms')
    expect(formatMs(1240)).toBe('1.24 s')
    expect(formatMs(12_345)).toBe('12.3 s')
  })
})

describe('formatUptime', () => {
  it('never rounds up to 100%', () => {
    expect(formatUptime(99.996)).toBe('99.99%')
    expect(formatUptime(100)).toBe('100%')
    expect(formatUptime(0)).toBe('0.00%')
    expect(formatUptime(null)).toBeNull()
  })
})

describe('formatWhen', () => {
  const now = new Date('2026-11-02T15:00:00Z') // tests run with TZ=UTC

  it('names today and yesterday', () => {
    expect(formatWhen('2026-11-02T14:05:00Z', now, 'en-GB')).toBe('Today 14:05')
    expect(formatWhen('2026-11-01T09:12:00Z', now, 'en-GB')).toBe('Yesterday 09:12')
  })

  it('shows the date, and the year only when it differs', () => {
    expect(formatWhen('2026-10-28T14:05:00Z', now, 'en-US')).toBe('Oct 28, 2:05 PM')
    expect(formatWhen('2025-12-30T14:05:00Z', now, 'en-US')).toBe('Dec 30, 2025, 2:05 PM')
  })

  it('shows seconds on request', () => {
    expect(formatWhen('2026-11-02T14:05:41Z', now, 'en-GB', 'medium')).toBe('Today 14:05:41')
  })
})

describe('plural', () => {
  it('picks the form by count', () => {
    expect(plural(1, 'site')).toBe('1 site')
    expect(plural(3, 'site')).toBe('3 sites')
    expect(plural(0, 'site')).toBe('0 sites')
  })
})
