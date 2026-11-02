import { ApiError } from '../api/client'

export interface FormErrors {
  /** Messages keyed by the body field they belong to. */
  fields: Record<string, string>
  /** A message that belongs to no single field. */
  form: string | null
}

interface ValidationItem {
  loc: (string | number)[]
  msg: string
}

function isValidationList(detail: unknown): detail is ValidationItem[] {
  return (
    Array.isArray(detail) &&
    detail.every((d) => typeof d === 'object' && d !== null && 'loc' in d && 'msg' in d)
  )
}

/** Pydantic prefixes custom validator messages; users don't need that. */
function clean(msg: string): string {
  const text = msg.replace(/^(Value error|Assertion failed), /, '')
  return text.charAt(0).toUpperCase() + text.slice(1)
}

/** Splits a 422 into per-field messages; any other error becomes one form message. */
export function formErrors(error: unknown): FormErrors {
  if (error instanceof ApiError && isValidationList(error.detail)) {
    const fields: Record<string, string> = {}
    const rest: string[] = []
    for (const item of error.detail) {
      // loc is ["body", field, ...index]; a model-level error is just ["body"].
      const field = item.loc[0] === 'body' ? item.loc[1] : undefined
      if (typeof field === 'string' && !(field in fields)) fields[field] = clean(item.msg)
      else if (field === undefined) rest.push(clean(item.msg))
    }
    return { fields, form: rest.length ? rest.join(' ') : null }
  }
  return { fields: {}, form: errorMessage(error) }
}

/** One sentence for an error shown to the user. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status >= 500) return 'The server ran into a problem. Try again in a moment.'
    if (typeof error.detail === 'string') return clean(error.detail) + '.'
    return `The request was refused (${error.status}).`
  }
  if (error instanceof TypeError) return "Can't reach the server. Check your connection."
  return 'Something went wrong.'
}
