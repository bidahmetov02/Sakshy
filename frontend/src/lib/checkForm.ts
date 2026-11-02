/** The check form's values, validation and request body. Pure, so it can be tested. */
import type { Check, CheckIn } from '../api/client'
import { formatInterval } from './format'

export interface CheckFormValues {
  name: string
  url: string
  interval_s: string
  timeout_s: string
  expected_status: string
  expected_body: string
  group_id: string
  alert_emails: string
}

export type CheckFormErrors = Partial<Record<keyof CheckFormValues, string>>

export const MAX_TIMEOUT_S = 30
const INTERVALS = [30, 60, 120, 300, 600, 900, 1800, 3600]

/** The interval choices, plus a check's own value if it isn't one of them. */
export function intervalOptions(current?: number): { value: number; label: string }[] {
  const values = current && !INTERVALS.includes(current) ? [...INTERVALS, current].sort((a, b) => a - b) : INTERVALS
  return values.map((value) => ({ value, label: formatInterval(value) }))
}

export function initialValues(check: Check | undefined, accountEmail: string): CheckFormValues {
  return {
    name: check?.name ?? '',
    url: check?.url ?? '',
    interval_s: String(check?.interval_s ?? 60),
    timeout_s: String(check?.timeout_s ?? 10),
    expected_status: String(check?.expected_status ?? 200),
    expected_body: check?.expected_body ?? '',
    group_id: check?.group_id ? String(check.group_id) : '',
    alert_emails: (check?.alert_emails ?? [accountEmail]).join('\n'),
  }
}

/** A URL typed without a scheme gets https://, the common case. */
export function normalizeUrl(url: string): string {
  const trimmed = url.trim()
  if (!trimmed || /^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed)) return trimmed
  return `https://${trimmed}`
}

export function parseEmails(text: string): string[] {
  return text
    .split(/[\s,;]+/)
    .map((e) => e.trim())
    .filter(Boolean)
}

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

export function validate(v: CheckFormValues): CheckFormErrors {
  const errors: CheckFormErrors = {}
  if (!v.name.trim()) errors.name = 'Give the check a name.'
  else if (v.name.trim().length > 200) errors.name = 'Keep the name under 200 characters.'

  const url = normalizeUrl(v.url)
  if (!url) errors.url = 'Enter the address to check.'
  else {
    try {
      const parsed = new URL(url)
      if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') errors.url = 'Use an http:// or https:// address.'
      else if (!parsed.hostname) errors.url = 'Enter a full address, like https://example.com.'
    } catch {
      errors.url = 'Enter a full address, like https://example.com.'
    }
  }

  const interval = Number(v.interval_s)
  const timeout = Number(v.timeout_s)
  if (!v.timeout_s.trim() || !Number.isFinite(timeout) || timeout < 0.01 || timeout > MAX_TIMEOUT_S) {
    errors.timeout_s = `Use a timeout between 0.01 and ${MAX_TIMEOUT_S} seconds.`
  } else if (timeout >= interval) {
    errors.timeout_s = 'The timeout must be shorter than the interval.'
  }

  const status = Number(v.expected_status)
  if (!Number.isInteger(status) || status < 100 || status > 599) {
    errors.expected_status = 'Use a status code from 100 to 599.'
  }
  if (v.expected_body.length > 1000) errors.expected_body = 'Keep the expected text under 1,000 characters.'

  if (!v.group_id) {
    const emails = parseEmails(v.alert_emails)
    const bad = emails.find((e) => !EMAIL.test(e))
    if (!emails.length) errors.alert_emails = 'Add at least one email address.'
    else if (bad) errors.alert_emails = `“${bad}” isn't an email address.`
    else if (emails.length > 20) errors.alert_emails = 'Add at most 20 addresses.'
  }
  return errors
}

/**
 * The body for POST, and for PATCH (the whole config). A grouped check's own list is
 * kept but unused; the API still requires one, so an empty list falls back to the
 * account's email.
 */
export function toPayload(v: CheckFormValues, accountEmail: string): CheckIn {
  const emails = parseEmails(v.alert_emails)
  return {
    name: v.name.trim(),
    url: normalizeUrl(v.url),
    interval_s: Number(v.interval_s),
    timeout_s: Number(v.timeout_s),
    expected_status: Number(v.expected_status),
    expected_body: v.expected_body === '' ? null : v.expected_body,
    group_id: v.group_id ? Number(v.group_id) : null,
    alert_emails: emails.length ? emails : [accountEmail],
  }
}
