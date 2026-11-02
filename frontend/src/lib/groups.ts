/** Group and public-page status wording. Pure, so it can be tested. */
import type { Group, GroupStatus } from '../api/client'
import { parseEmails } from './checkForm'
import type { DisplayState } from './checks'
import { plural } from './format'

/** Status reads with the glyph of the check state it summarises. */
export const STATUS_GLYPH: Record<GroupStatus, DisplayState> = {
  operational: 'up',
  partial_outage: 'failing',
  major_outage: 'down',
}

export const STATUS_LABEL: Record<GroupStatus, string> = {
  operational: 'Operational',
  partial_outage: 'Partial outage',
  major_outage: 'Major outage',
}

/** "5 checks, 2 down", "1 check", "No checks yet". */
export function groupCounts(group: Pick<Group, 'check_count' | 'down_count'>): string {
  if (!group.check_count) return 'No checks yet'
  const base = plural(group.check_count, 'check')
  return group.down_count ? `${base}, ${group.down_count} down` : base
}

export interface GroupFormValues {
  name: string
  alert_emails: string
}

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

export function validateGroup(v: GroupFormValues): Partial<Record<keyof GroupFormValues, string>> {
  const errors: Partial<Record<keyof GroupFormValues, string>> = {}
  if (!v.name.trim()) errors.name = 'Give the group a name.'
  else if (v.name.trim().length > 200) errors.name = 'Keep the name under 200 characters.'
  const emails = parseEmails(v.alert_emails)
  const bad = emails.find((e) => !EMAIL.test(e))
  if (!emails.length) errors.alert_emails = 'Add at least one email address.'
  else if (bad) errors.alert_emails = `“${bad}” isn't an email address.`
  else if (emails.length > 20) errors.alert_emails = 'Add at most 20 addresses.'
  return errors
}
