import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router'
import { useMe } from '../api/queries'
import { errorMessage } from '../lib/errors'

/** Renders its children for a signed-in user; sends anyone else to sign in. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const me = useMe()
  const location = useLocation()

  if (me.isPending) return <div className="boot" aria-busy="true" />
  if (me.isError) {
    return (
      <main className="boot">
        <p>{errorMessage(me.error)}</p>
        <button type="button" className="button" onClick={() => void me.refetch()}>
          Try again
        </button>
      </main>
    )
  }
  if (!me.data) return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  return children
}
