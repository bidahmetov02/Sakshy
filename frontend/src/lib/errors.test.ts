import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { errorMessage, formErrors } from './errors'

describe('formErrors', () => {
  it('maps 422 items to their body fields', () => {
    const error = new ApiError(422, [
      { loc: ['body', 'url'], msg: 'Value error, must be an http:// or https:// URL', type: 'value_error' },
      { loc: ['body', 'alert_emails', 1], msg: 'value is not a valid email address', type: 'value_error' },
      { loc: ['body'], msg: 'Value error, timeout_s must be less than interval_s', type: 'value_error' },
    ])
    expect(formErrors(error)).toEqual({
      fields: { url: 'Must be an http:// or https:// URL', alert_emails: 'Value is not a valid email address' },
      form: 'Timeout_s must be less than interval_s',
    })
  })

  it('turns any other error into a form message', () => {
    expect(formErrors(new ApiError(422, 'group not found'))).toEqual({ fields: {}, form: 'Group not found.' })
  })
})

describe('errorMessage', () => {
  it('explains server, network and plain errors', () => {
    expect(errorMessage(new ApiError(503, 'x'))).toMatch(/server ran into a problem/)
    expect(errorMessage(new TypeError('Failed to fetch'))).toMatch(/Can't reach the server/)
    expect(errorMessage(new ApiError(409, { foo: 1 }))).toBe('The request was refused (409).')
    expect(errorMessage(new Error('?'))).toBe('Something went wrong.')
  })
})
