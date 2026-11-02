import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router'
import { useCreateStatusPage, useStatusPages } from '../api/queries'
import { Field } from '../components/Field'
import { errorMessage, formErrors } from '../lib/errors'
import { plural } from '../lib/format'
import { publicPath } from '../lib/statusPages'

export function StatusPagesPage() {
  const pages = useStatusPages()

  return (
    <>
      <div className="page-head">
        <h1 className="page-title">Status pages</h1>
        <Link to="/status-pages/new" className="button button-primary">
          New status page
        </Link>
      </div>
      <p className="page-intro">
        A public page that shows your visitors whether your sites are up, without addresses or error details.
      </p>

      {pages.isPending && <p className="muted">Loading…</p>}
      {pages.isError && <p className="form-error">{errorMessage(pages.error)}</p>}
      {pages.data?.length === 0 && <p className="muted empty">No status pages yet.</p>}
      {!!pages.data?.length && (
        <ul className="rows">
          {pages.data.map((p) => (
            <li key={p.id} className="row">
              <Link to={`/status-pages/${p.id}`} className="row-link row-grid-page">
                <span className="row-name">{p.title}</span>
                <span className="row-meta">{publicPath(p.slug)}</span>
                <span className="row-meta">{p.items.length ? plural(p.items.length, 'check') : 'No checks yet'}</span>
                <span className="row-status">{p.is_enabled ? 'Published' : 'Hidden'}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

export function NewStatusPage() {
  const create = useCreateStatusPage()
  const navigate = useNavigate()
  const [title, setTitle] = useState('')
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!title.trim()) {
      setError('Give the page a title.')
      return
    }
    setError(null)
    try {
      const page = await create.mutateAsync({
        title: title.trim(),
        show_uptime: true,
        show_response_times: false,
        show_incidents: false,
      })
      navigate(`/status-pages/${page.id}`, { replace: true })
    } catch (e) {
      const { fields, form } = formErrors(e)
      setError(fields.title ?? form)
    }
  }

  return (
    <>
      <Link to="/status-pages" className="back">
        Status pages
      </Link>
      <h1 className="page-title">New status page</h1>
      <form className="form" onSubmit={(e) => void submit(e)} noValidate>
        <Field id="title" label="Title" error={error ?? undefined} hint="Visitors see it at the top of the page.">
          <input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} autoFocus placeholder="Acme status" />
        </Field>
        <div className="form-actions">
          <button type="submit" className="button button-primary" disabled={create.isPending}>
            {create.isPending ? 'Creating…' : 'Create and add checks'}
          </button>
          <Link to="/status-pages" className="button">
            Cancel
          </Link>
        </div>
      </form>
    </>
  )
}
