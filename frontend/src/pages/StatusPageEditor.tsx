import { useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { ApiError, type StatusPage } from '../api/client'
import { useChecks, useDeleteStatusPage, useSaveStatusPage, useStatusPage } from '../api/queries'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { Field } from '../components/Field'
import { errorMessage, formErrors } from '../lib/errors'
import {
  draftItems,
  itemErrors,
  moveItem,
  publicPath,
  sameItems,
  toItemsIn,
  type DraftItem,
} from '../lib/statusPages'
import { NotFound } from './NotFound'

export function StatusPageEditor() {
  const id = Number(useParams().id)
  const page = useStatusPage(id)

  if (page.isPending) return <p className="muted">Loading…</p>
  if (page.isError) {
    if (page.error instanceof ApiError && page.error.status === 404) return <NotFound what="status page" />
    return <p className="form-error">{errorMessage(page.error)}</p>
  }
  return <Editor key={page.data.id} page={page.data} />
}

interface Settings {
  title: string
  is_enabled: boolean
  show_uptime: boolean
  show_response_times: boolean
  show_incidents: boolean
}

function settingsOf(page: StatusPage): Settings {
  const { title, is_enabled, show_uptime, show_response_times, show_incidents } = page
  return { title, is_enabled, show_uptime, show_response_times, show_incidents }
}

function Editor({ page }: { page: StatusPage }) {
  const checks = useChecks()
  const save = useSaveStatusPage(page.id)
  const [settings, setSettings] = useState<Settings>(() => settingsOf(page))
  const [items, setItems] = useState<DraftItem[]>(() => draftItems(page))
  const [titleError, setTitleError] = useState<string | null>(null)
  const [nameErrors, setNameErrors] = useState<Record<number, string>>({})
  const [formError, setFormError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  const savedItems = draftItems(page)
  const itemsChanged = !sameItems(items, savedItems)
  const dirty = itemsChanged || JSON.stringify(settings) !== JSON.stringify(settingsOf(page))
  const checkName = new Map(checks.data?.map((c) => [c.id, c.name]))
  const onPage = new Set(items.map((i) => i.check_id))
  const addable = (checks.data ?? []).filter((c) => !onPage.has(c.id)).sort((a, b) => a.name.localeCompare(b.name))

  const toggle = (flag: keyof Omit<Settings, 'title'>) => setSettings((s) => ({ ...s, [flag]: !s[flag] }))
  const setName = (checkId: number, name: string) =>
    setItems((list) => list.map((i) => (i.check_id === checkId ? { ...i, display_name: name } : i)))

  async function submit(event: FormEvent) {
    event.preventDefault()
    setSaved(false)
    setFormError(null)
    const errors = itemErrors(items)
    const title = settings.title.trim() ? null : 'Give the page a title.'
    setTitleError(title)
    setNameErrors(errors)
    if (title || Object.keys(errors).length) return
    try {
      await save.mutateAsync({
        patch: { ...settings, title: settings.title.trim() },
        items: itemsChanged ? toItemsIn(items) : undefined,
      })
      // What the server kept, so the form reads as saved once the page is refetched.
      setSettings((s) => ({ ...s, title: s.title.trim() }))
      setItems((l) => l.map((i) => ({ ...i, display_name: i.display_name.trim() })))
      setSaved(true)
    } catch (e) {
      const { fields, form } = formErrors(e)
      setTitleError(fields.title ?? null)
      setFormError(form ?? (Object.keys(fields).length ? Object.values(fields).join(' ') : null))
    }
  }

  return (
    <>
      <Link to="/status-pages" className="back">
        Status pages
      </Link>
      <header className="detail-head">
        <div className="detail-title">
          <h1 className="page-title">{page.title}</h1>
          <PublicLink page={page} />
        </div>
        <DeletePage page={page} />
      </header>

      <form className="form form-wide section section-tight" onSubmit={(e) => void submit(e)} noValidate>
        <Field id="title" label="Title" error={titleError ?? undefined}>
          <input
            value={settings.title}
            maxLength={200}
            onChange={(e) => setSettings((s) => ({ ...s, title: e.target.value }))}
          />
        </Field>

        <fieldset className="fieldset">
          <legend>Visitors see</legend>
          <label className="pick">
            <input type="checkbox" checked={settings.show_uptime} onChange={() => toggle('show_uptime')} />
            Uptime over the last 30 days
          </label>
          <label className="pick">
            <input type="checkbox" checked={settings.show_response_times} onChange={() => toggle('show_response_times')} />
            Response times
          </label>
          <label className="pick">
            <input type="checkbox" checked={settings.show_incidents} onChange={() => toggle('show_incidents')} />
            Recent incidents
          </label>
        </fieldset>

        <fieldset className="fieldset">
          <legend>Checks on the page</legend>
          <p className="field-hint">
            Visitors see each check by its public name, never its address. They appear in this order.
          </p>
          {items.length === 0 && <p className="muted">No checks on this page yet.</p>}
          {items.length > 0 && (
            <ol className="item-list">
              {items.map((item, index) => (
                <li key={item.check_id} className="item-row">
                  <div className="field item-field">
                    <label htmlFor={`item-${item.check_id}`}>
                      <span className="visually-hidden">Public name for </span>
                      {checkName.get(item.check_id) ?? 'Check'}
                    </label>
                    <input
                      id={`item-${item.check_id}`}
                      value={item.display_name}
                      maxLength={200}
                      onChange={(e) => setName(item.check_id, e.target.value)}
                      aria-invalid={!!nameErrors[item.check_id] || undefined}
                    />
                    {nameErrors[item.check_id] && <p className="field-error">{nameErrors[item.check_id]}</p>}
                  </div>
                  <div className="item-buttons">
                    <button
                      type="button"
                      className="icon-button"
                      onClick={() => setItems((l) => moveItem(l, index, -1))}
                      disabled={index === 0}
                      aria-label={`Move ${item.display_name || 'item'} up`}
                    >
                      ↑
                    </button>
                    <button
                      type="button"
                      className="icon-button"
                      onClick={() => setItems((l) => moveItem(l, index, 1))}
                      disabled={index === items.length - 1}
                      aria-label={`Move ${item.display_name || 'item'} down`}
                    >
                      ↓
                    </button>
                    <button
                      type="button"
                      className="link-button"
                      onClick={() => setItems((l) => l.filter((i) => i.check_id !== item.check_id))}
                      aria-label={`Remove ${item.display_name || 'item'}`}
                    >
                      Remove
                    </button>
                  </div>
                </li>
              ))}
            </ol>
          )}
          {addable.length > 0 && (
            <label className="filter add-item">
              <span className="visually-hidden">Add a check</span>
              <select
                value=""
                onChange={(e) => {
                  const check = addable.find((c) => c.id === Number(e.target.value))
                  if (check) setItems((l) => [...l, { check_id: check.id, display_name: check.name }])
                }}
              >
                <option value="">Add a check…</option>
                {addable.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </label>
          )}
        </fieldset>

        <fieldset className="fieldset">
          <legend>Visibility</legend>
          <label className="pick">
            <input type="checkbox" checked={settings.is_enabled} onChange={() => toggle('is_enabled')} />
            Published
          </label>
          <p className="field-hint">
            Anyone with the link can see a published page. Hiding it keeps the link, so publishing it again brings it
            back at the same address.
          </p>
        </fieldset>

        {formError && (
          <p className="form-error" role="alert">
            {formError}
          </p>
        )}
        <div className="form-actions">
          <button type="submit" className="button button-primary" disabled={save.isPending || !dirty}>
            {save.isPending ? 'Saving…' : 'Save changes'}
          </button>
          {dirty ? (
            <span className="form-status">Unsaved changes</span>
          ) : (
            saved && (
              <span className="form-status" role="status">
                Saved
              </span>
            )
          )}
        </div>
      </form>
    </>
  )
}

function PublicLink({ page }: { page: StatusPage }) {
  const [copied, setCopied] = useState(false)
  const url = `${window.location.origin}${publicPath(page.slug)}`

  async function copy() {
    try {
      await navigator.clipboard.writeText(url)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      setCopied(false)
    }
  }

  return (
    <p className="detail-sub public-link">
      {page.is_enabled ? (
        <a href={publicPath(page.slug)} target="_blank" rel="noreferrer noopener">
          {url}
        </a>
      ) : (
        <span>
          {url} <span className="muted">(hidden)</span>
        </span>
      )}
      <button type="button" className="link-button" onClick={() => void copy()}>
        {copied ? 'Copied' : 'Copy link'}
      </button>
    </p>
  )
}

function DeletePage({ page }: { page: StatusPage }) {
  const remove = useDeleteStatusPage(page.id)
  const navigate = useNavigate()
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function del() {
    try {
      await remove.mutateAsync()
      navigate('/status-pages', { replace: true })
    } catch (e) {
      setConfirming(false)
      setError(errorMessage(e))
    }
  }

  return (
    <div className="actions">
      <div className="actions-row">
        <button type="button" className="button button-quiet-danger" onClick={() => setConfirming(true)}>
          Delete
        </button>
      </div>
      {error && <p className="form-error">{error}</p>}
      <ConfirmDialog
        open={confirming}
        title={`Delete ${page.title}?`}
        confirm="Delete page"
        danger
        busy={remove.isPending}
        onConfirm={() => void del()}
        onClose={() => setConfirming(false)}
      >
        <p>Its link stops working for everyone. Your checks are not affected.</p>
      </ConfirmDialog>
    </div>
  )
}
