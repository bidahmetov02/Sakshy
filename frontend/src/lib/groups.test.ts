import { describe, expect, it } from 'vitest'
import { groupCounts, validateGroup } from './groups'

describe('groupCounts', () => {
  it('counts members and down ones', () => {
    expect(groupCounts({ check_count: 0, down_count: 0 })).toBe('No checks yet')
    expect(groupCounts({ check_count: 1, down_count: 0 })).toBe('1 check')
    expect(groupCounts({ check_count: 5, down_count: 2 })).toBe('5 checks, 2 down')
  })
})

describe('validateGroup', () => {
  it('needs a name and valid emails', () => {
    expect(validateGroup({ name: 'Prod', alert_emails: 'a@x.com\nb@x.com' })).toEqual({})
    expect(validateGroup({ name: ' ', alert_emails: 'a@x.com' }).name).toBeDefined()
    expect(validateGroup({ name: 'Prod', alert_emails: '' }).alert_emails).toBe('Add at least one email address.')
    expect(validateGroup({ name: 'Prod', alert_emails: 'a@x.com, nope' }).alert_emails).toBe('“nope” isn\'t an email address.')
  })
})
