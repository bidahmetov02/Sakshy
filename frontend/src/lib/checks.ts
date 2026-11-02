import type { Check } from '../api/client'
import { formatDuration, plural } from './format'

/** What the owner sees. Paused isn't a stored state: it's `is_paused` over `pending`. */
export type DisplayState = 'down' | 'failing' | 'pending' | 'up' | 'paused'
export type Tone = 'down' | 'failing' | 'up' | 'neutral'

type CheckLike = Pick<
  Check,
  'id' | 'name' | 'state' | 'is_paused' | 'in_maintenance' | 'state_changed_at' | 'incident_started_at'
>

export function displayState(check: Pick<Check, 'state' | 'is_paused'>): DisplayState {
  return check.is_paused ? 'paused' : check.state
}

export const STATE_LABEL: Record<DisplayState, string> = {
  down: 'Down',
  failing: 'Failing',
  pending: 'Pending',
  up: 'Up',
  paused: 'Paused',
}

const SEVERITY: Record<DisplayState, number> = { down: 0, failing: 1, pending: 2, up: 3, paused: 4 }

/** Worst first, so what needs attention is at the top; then by name. */
export function sortChecks<T extends CheckLike>(checks: readonly T[]): T[] {
  return [...checks].sort(
    (a, b) =>
      SEVERITY[displayState(a)] - SEVERITY[displayState(b)] ||
      a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }) ||
      a.id - b.id,
  )
}

export interface Summary {
  tone: Tone
  headline: string
  detail: string | null
}

/** The dashboard's opening sentence: is anything down, and what else to know. */
export function summarize(checks: readonly CheckLike[]): Summary {
  if (!checks.length) {
    return {
      tone: 'neutral',
      headline: 'No sites watched yet.',
      detail: 'Add a check and Sakshy will start watching it right away.',
    }
  }
  const active = checks.filter((c) => !c.is_paused)
  const paused = checks.length - active.length
  const pausedNote = paused ? `${plural(paused, 'check')} paused.` : null
  const n = active.length
  if (!n) return { tone: 'neutral', headline: `All ${plural(paused, 'check')} are paused.`, detail: null }

  const count = (state: DisplayState) => active.filter((c) => c.state === state).length
  const down = count('down')
  const failing = count('failing')
  const pending = count('pending')
  const join = (...parts: (string | null)[]) => parts.filter(Boolean).join(' ') || null
  const of = (k: number, what: string) =>
    k === n
      ? n === 1
        ? `Your site is ${what}.`
        : `All ${n} sites are ${what}.`
      : `${k} of ${plural(n, 'site')} ${k === 1 ? 'is' : 'are'} ${what}.`

  if (down) {
    const held = active.filter((c) => c.state === 'down' && c.in_maintenance).length
    const heldNote = held
      ? `${held === down ? (down === 1 ? 'It is' : 'They are') : held} in maintenance, so alerts are held back.`
      : null
    const failingNote = failing ? `${failing} more ${failing === 1 ? 'is' : 'are'} failing.` : null
    return { tone: 'down', headline: of(down, 'down'), detail: join(heldNote, failingNote, pausedNote) }
  }
  if (failing) {
    return {
      tone: 'failing',
      headline: of(failing, 'failing'),
      detail: join('Re-checking before raising an alert.', pausedNote),
    }
  }
  if (pending === n) {
    return {
      tone: 'neutral',
      headline: n === 1 ? 'Checking your site for the first time.' : `Checking ${n} sites for the first time.`,
      detail: pausedNote,
    }
  }
  const pendingNote = pending
    ? `${pending} ${pending === 1 ? 'is' : 'are'} waiting for a first check.`
    : null
  const up = n - pending
  return {
    tone: 'up',
    headline: pending ? `${plural(up, 'site')} ${up === 1 ? 'is' : 'are'} up.` : of(n, 'up'),
    detail: join(pendingNote, pausedNote),
  }
}

/** How long the check has been in its state, for the list: "Down 12 min", "Up 4 d". */
export function stateSince(check: CheckLike, now: number): string {
  switch (displayState(check)) {
    case 'paused':
      return 'Paused'
    case 'pending':
      return 'Waiting for first check'
    case 'failing':
      return 'Failing, re-checking'
    case 'down':
      return check.incident_started_at
        ? `Down ${formatDuration(now - Date.parse(check.incident_started_at))}`
        : 'Down'
    case 'up':
      return `Up ${formatDuration(now - Date.parse(check.state_changed_at))}`
  }
}

export const ERROR_LABEL: Record<NonNullable<Check['last_error_kind']>, string> = {
  timeout: 'Timed out',
  dns: "Domain name didn't resolve",
  connect: 'Connection failed',
  tls: 'TLS error',
  status_mismatch: 'Unexpected status code',
  body_mismatch: 'Expected text not found',
  too_large: 'Response too large',
  other: 'Request failed',
}

export const END_REASON_LABEL: Record<'recovered' | 'paused' | 'check_deleted', string> = {
  recovered: 'Recovered',
  paused: 'Ended by pausing',
  check_deleted: 'Ended by deleting',
}

/** The failure, as the user reads it: "Connection failed: connection refused". */
export function describeError(
  kind: Check['last_error_kind'] | undefined,
  message: string | null | undefined,
  statusCode?: number | null,
): string | null {
  if (!kind) return null
  // With the code known, "Got status 503" says it all; without it, the message does.
  if (kind === 'status_mismatch' && statusCode) return `Got status ${statusCode}`
  return message ? `${ERROR_LABEL[kind]}: ${message}` : ERROR_LABEL[kind]
}

/** The detail page's status line: how long, and why. */
export function statusSentence(check: Check, now: number): string {
  const error = describeError(check.last_error_kind, check.last_error_message, check.last_status_code)
  switch (displayState(check)) {
    case 'paused':
      return 'Paused. Nothing is checked until you resume it.'
    case 'pending':
      return 'Waiting for its first check.'
    case 'failing':
      return `Failing. Re-checking before raising an alert.${error ? ` ${error}.` : ''}`
    case 'down': {
      const since = check.incident_started_at
        ? `Down for ${formatDuration(now - Date.parse(check.incident_started_at))}.`
        : 'Down.'
      return `${since}${error ? ` ${error}.` : ''}`
    }
    case 'up': {
      const last = [
        check.last_response_ms != null ? `Last check took ${check.last_response_ms} ms` : null,
        check.last_status_code ? `status ${check.last_status_code}` : null,
      ].filter(Boolean)
      return `Up for ${formatDuration(now - Date.parse(check.state_changed_at))}.${last.length ? ` ${last.join(', ')}.` : ''}`
    }
  }
}
