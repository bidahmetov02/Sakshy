import createClient, { type Middleware } from 'openapi-fetch'
import type { components, paths } from './schema'

type Schemas = components['schemas']
export type Check = Schemas['CheckOut']
export type CheckIn = Schemas['CheckIn']
export type CheckPatch = Schemas['CheckPatch']
export type Group = Schemas['GroupOut']
export type Stats = Schemas['Stats']
export type Bucket = Schemas['Bucket']
export type Result = Schemas['ResultOut']
export type Incident = Schemas['IncidentOut']
export type User = Schemas['UserOut']
export type Health = Schemas['Health']
export type Range = 'day' | 'week' | 'month'

/** A non-2xx response. `detail` is FastAPI's: a message, or a list of validation errors. */
export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, detail: unknown) {
    super(typeof detail === 'string' ? detail : `request failed (${status})`)
    this.status = status
    this.detail = detail
  }
}

let onUnauthorized = () => {}

/** Called on any 401 except a failed login: the session has ended. */
export function setUnauthorizedHandler(handler: () => void) {
  onUnauthorized = handler
}

const sessionGuard: Middleware = {
  onResponse({ request, response }) {
    if (response.status === 401 && !request.url.endsWith('/api/auth/login')) onUnauthorized()
  },
}

// Paths already carry the `/api` prefix; the SPA and the API share an origin.
export const api = createClient<paths>({ baseUrl: '', credentials: 'same-origin' })
api.use(sessionGuard)

/** Unwraps an openapi-fetch result: the body on success, an ApiError otherwise. */
export async function unwrap<T>(
  request: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await request
  if (!response.ok) {
    const detail = (error as { detail?: unknown } | undefined)?.detail ?? error
    throw new ApiError(response.status, detail)
  }
  return data as T
}
