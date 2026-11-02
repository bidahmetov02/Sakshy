import { describe, expect, it } from 'vitest'
import { initialValues, intervalOptions, normalizeUrl, parseEmails, toPayload, validate, type CheckFormValues } from './checkForm'

const base: CheckFormValues = { ...initialValues(undefined, 'me@x.com'), name: 'Shop', url: 'https://shop.example.com' }

describe('initialValues', () => {
  it('pre-fills the account email and sensible defaults', () => {
    expect(initialValues(undefined, 'me@x.com')).toMatchObject({
      interval_s: '60',
      timeout_s: '10',
      expected_status: '200',
      alert_emails: 'me@x.com',
      group_id: '',
    })
  })
})

describe('intervalOptions', () => {
  it('keeps a custom interval selectable', () => {
    expect(intervalOptions().map((o) => o.value)).not.toContain(45)
    const options = intervalOptions(45)
    expect(options.map((o) => o.value).slice(0, 3)).toEqual([30, 45, 60])
    expect(options[1].label).toBe('45 s')
  })
})

describe('normalizeUrl', () => {
  it('adds https:// only when there is no scheme', () => {
    expect(normalizeUrl(' example.com/health ')).toBe('https://example.com/health')
    expect(normalizeUrl('http://example.com')).toBe('http://example.com')
    expect(normalizeUrl('')).toBe('')
  })
})

describe('parseEmails', () => {
  it('splits on lines, commas and spaces', () => {
    expect(parseEmails('a@x.com, b@x.com\n\nc@x.com;d@x.com')).toEqual(['a@x.com', 'b@x.com', 'c@x.com', 'd@x.com'])
  })
})

describe('validate', () => {
  it('accepts a valid form', () => {
    expect(validate(base)).toEqual({})
  })

  it('requires a name and a usable URL', () => {
    expect(validate({ ...base, name: '  ' }).name).toBeDefined()
    expect(validate({ ...base, url: '' }).url).toBeDefined()
    expect(validate({ ...base, url: 'ftp://example.com' }).url).toBe('Use an http:// or https:// address.')
    expect(validate({ ...base, url: 'example.com' })).toEqual({}) // https:// is assumed
  })

  it('bounds the timeout by 30 s and by the interval', () => {
    expect(validate({ ...base, timeout_s: '31' }).timeout_s).toMatch(/between/)
    expect(validate({ ...base, timeout_s: '' }).timeout_s).toMatch(/between/)
    expect(validate({ ...base, interval_s: '30', timeout_s: '30' }).timeout_s).toBe(
      'The timeout must be shorter than the interval.',
    )
  })

  it('checks the status code', () => {
    expect(validate({ ...base, expected_status: '99' }).expected_status).toBeDefined()
    expect(validate({ ...base, expected_status: '200.5' }).expected_status).toBeDefined()
  })

  it("checks the emails only while ungrouped, since a group's list replaces them", () => {
    expect(validate({ ...base, alert_emails: '' }).alert_emails).toBe('Add at least one email address.')
    expect(validate({ ...base, alert_emails: 'a@x.com, nope' }).alert_emails).toBe('“nope” isn\'t an email address.')
    expect(validate({ ...base, alert_emails: '', group_id: '3' })).toEqual({})
  })
})

describe('toPayload', () => {
  it('converts the form to the API body', () => {
    expect(
      toPayload({ ...base, url: 'shop.example.com', expected_body: '', alert_emails: 'a@x.com\nb@x.com' }, 'me@x.com'),
    ).toEqual({
      name: 'Shop',
      url: 'https://shop.example.com',
      interval_s: 60,
      timeout_s: 10,
      expected_status: 200,
      expected_body: null,
      group_id: null,
      alert_emails: ['a@x.com', 'b@x.com'],
    })
  })

  it("falls back to the account email for a grouped check's unused list", () => {
    expect(toPayload({ ...base, group_id: '3', alert_emails: '' }, 'me@x.com')).toMatchObject({
      group_id: 3,
      alert_emails: ['me@x.com'],
    })
  })
})
