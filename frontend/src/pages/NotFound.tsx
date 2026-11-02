import { Link } from 'react-router'

/** Also what another user's check looks like: the API answers 404 for both. */
export function NotFound({ what = 'page' }: { what?: string }) {
  return (
    <section className="not-found">
      <h1 className="page-title">This {what} doesn't exist</h1>
      <p className="muted">It may have been deleted, or the link is wrong.</p>
      <Link to="/" className="button">
        Back to checks
      </Link>
    </section>
  )
}
