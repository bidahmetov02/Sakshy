/** Maintenance window times: form values, request bodies and wording. Pure, so it can be tested. */
import type { MaintenanceIn, MaintenancePatch, MaintenanceWindow } from '../api/client'
import { formatDuration, formatWhen } from './format'

const MINUTE = 60_000

export const DURATIONS = [
  { minutes: 30, label: '30 min' },
  { minutes: 60, label: '1 hour' },
  { minutes: 120, label: '2 hours' },
  { minutes: 240, label: '4 hours' },
  { minutes: 480, label: '8 hours' },
]

/** A time as a `datetime-local` input value, in the browser's time zone. */
export function toLocalInput(ms: number): string {
  const d = new Date(ms)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** A `datetime-local` value read as local time; NaN if it isn't one. */
export function fromLocalInput(value: string): number {
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(value) ? new Date(value).getTime() : Number.NaN
}

export interface WindowFormValues {
  /** "c:<check id>" or "g:<group id>". */
  target: string
  start: 'now' | 'at'
  startsAt: string
  /** Minutes, or "custom" to use `endsAt`. */
  duration: string
  endsAt: string
  reason: string
}

export type WindowFormErrors = Partial<Record<keyof WindowFormValues, string>>

export function newWindowValues(now: number, target = ''): WindowFormValues {
  const nextHour = Math.ceil(now / (60 * MINUTE)) * 60 * MINUTE
  return {
    target,
    start: 'now',
    startsAt: toLocalInput(nextHour),
    duration: '60',
    endsAt: toLocalInput(nextHour + 60 * MINUTE),
    reason: '',
  }
}

function checkReason(reason: string, errors: WindowFormErrors) {
  if (reason.trim().length > 500) errors.reason = 'Keep the reason under 500 characters.'
}

/** The POST body for a new window, or what's wrong with the form. */
export function buildWindow(v: WindowFormValues, now: number): { body?: MaintenanceIn; errors: WindowFormErrors } {
  const errors: WindowFormErrors = {}
  const [kind, rawId] = v.target.split(':')
  const id = Number(rawId)
  if ((kind !== 'c' && kind !== 'g') || !Number.isInteger(id)) errors.target = 'Choose a check or a group.'

  const starts = v.start === 'now' ? now : fromLocalInput(v.startsAt)
  if (Number.isNaN(starts)) errors.startsAt = 'Enter a start date and time.'

  const ends = v.duration === 'custom' ? fromLocalInput(v.endsAt) : starts + Number(v.duration) * MINUTE
  if (v.duration === 'custom' && Number.isNaN(ends)) errors.endsAt = 'Enter an end date and time.'
  else if (!Number.isNaN(starts) && ends <= starts) errors.endsAt = 'The end must be after the start.'
  else if (ends <= now) errors.endsAt = 'The end must be in the future.'
  checkReason(v.reason, errors)

  if (Object.keys(errors).length) return { errors }
  const reason = v.reason.trim()
  return {
    errors,
    body: {
      ...(kind === 'c' ? { check_id: id } : { group_id: id }),
      // A start in the past means "now" to the API, so a slow clock still starts it now.
      starts_at: new Date(starts).toISOString(),
      ends_at: new Date(ends).toISOString(),
      ...(reason ? { reason } : {}),
    },
  }
}

export interface WindowEditValues {
  startsAt: string
  endsAt: string
  reason: string
}

export function editWindowValues(w: MaintenanceWindow): WindowEditValues {
  return { startsAt: toLocalInput(Date.parse(w.starts_at)), endsAt: toLocalInput(Date.parse(w.ends_at)), reason: w.reason ?? '' }
}

/** The PATCH body: the start only while the window hasn't started; an empty reason clears it. */
export function buildWindowPatch(
  v: WindowEditValues,
  w: MaintenanceWindow,
): { body?: MaintenancePatch; errors: Partial<Record<keyof WindowEditValues, string>> } {
  const errors: Partial<Record<keyof WindowEditValues, string>> = {}
  const upcoming = w.status === 'upcoming'
  const starts = upcoming ? fromLocalInput(v.startsAt) : Date.parse(w.starts_at)
  const ends = fromLocalInput(v.endsAt)
  if (Number.isNaN(starts)) errors.startsAt = 'Enter a start date and time.'
  if (Number.isNaN(ends)) errors.endsAt = 'Enter an end date and time.'
  else if (!Number.isNaN(starts) && ends <= starts) errors.endsAt = 'The end must be after the start.'
  checkReason(v.reason, errors)
  if (Object.keys(errors).length) return { errors }
  return {
    errors,
    body: {
      ...(upcoming ? { starts_at: new Date(starts).toISOString() } : {}),
      ends_at: new Date(ends).toISOString(),
      reason: v.reason.trim() || null,
    },
  }
}

function sameDay(a: number, b: number): boolean {
  return new Date(a).toDateString() === new Date(b).toDateString()
}

/** "Today 14:00–16:00", or "Today 22:00 to Nov 3, 02:00" across days. */
export function windowRange(w: Pick<MaintenanceWindow, 'starts_at' | 'ends_at'>, now: Date = new Date(), locale?: string): string {
  const start = Date.parse(w.starts_at)
  const end = Date.parse(w.ends_at)
  const from = formatWhen(w.starts_at, now, locale)
  if (sameDay(start, end)) {
    return `${from}–${new Date(end).toLocaleTimeString(locale, { timeStyle: 'short' })}`
  }
  return `${from} to ${formatWhen(w.ends_at, now, locale)}`
}

/** Where the window is in time, as seen now (the server's status may lag by a refetch). */
export function windowPhase(w: Pick<MaintenanceWindow, 'starts_at' | 'ends_at'>, now: number): 'upcoming' | 'active' | 'past' {
  if (now < Date.parse(w.starts_at)) return 'upcoming'
  return now < Date.parse(w.ends_at) ? 'active' : 'past'
}

/** "Ends in 1 h 20 min", "Starts in 2 h", "Ended 3 d ago". */
export function windowTiming(w: Pick<MaintenanceWindow, 'starts_at' | 'ends_at'>, now: number): string {
  const start = Date.parse(w.starts_at)
  const end = Date.parse(w.ends_at)
  switch (windowPhase(w, now)) {
    case 'upcoming':
      return `Starts in ${formatDuration(start - now)}`
    case 'active':
      return `Ends in ${formatDuration(end - now)}`
    case 'past':
      return `Ended ${formatDuration(now - end)} ago`
  }
}
