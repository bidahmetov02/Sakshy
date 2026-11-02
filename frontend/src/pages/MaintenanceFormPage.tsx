import { useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'
import { ApiError, type MaintenanceWindow } from '../api/client'
import { useChecks, useCreateWindow, useGroups, useMaintenance, useUpdateWindow } from '../api/queries'
import { Field } from '../components/Field'
import { formErrors } from '../lib/errors'
import {
  DURATIONS,
  buildWindow,
  buildWindowPatch,
  editWindowValues,
  newWindowValues,
  windowPhase,
  windowRange,
  type WindowEditValues,
  type WindowFormErrors,
  type WindowFormValues,
} from '../lib/maintenance'
import { useNow } from '../lib/useNow'
import { NotFound } from './NotFound'

/** Server field names to form field names. */
const FIELD_OF: Record<string, keyof WindowFormValues> = {
  check_id: 'target',
  group_id: 'target',
  starts_at: 'startsAt',
  ends_at: 'endsAt',
  reason: 'reason',
}

function mapErrors(e: unknown): { fields: WindowFormErrors; form: string | null } {
  const { fields, form } = formErrors(e)
  const mapped: WindowFormErrors = {}
  const rest: string[] = form ? [form] : []
  for (const [field, message] of Object.entries(fields)) {
    const to = FIELD_OF[field]
    if (to) mapped[to] = message
    else rest.push(message)
  }
  return { fields: mapped, form: rest.length ? rest.join(' ') : null }
}

export function NewWindow() {
  const [params] = useSearchParams()
  const checks = useChecks()
  const groups = useGroups()
  const create = useCreateWindow()
  const navigate = useNavigate()
  const [values, setValues] = useState<WindowFormValues>(() => newWindowValues(Date.now(), params.get('target') ?? ''))
  const [errors, setErrors] = useState<WindowFormErrors>({})
  const [formError, setFormError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const set = <K extends keyof WindowFormValues>(field: K, value: WindowFormValues[K]) =>
    setValues((v) => ({ ...v, [field]: value }))

  async function submit(event: FormEvent) {
    event.preventDefault()
    const { body, errors: found } = buildWindow(values, Date.now())
    setErrors(found)
    setFormError(null)
    if (!body) {
      document.getElementById(`f-${Object.keys(found)[0]}`)?.focus()
      return
    }
    setBusy(true)
    try {
      await create.mutateAsync(body)
      navigate('/maintenance')
    } catch (e) {
      const { fields, form } = mapErrors(e)
      setErrors(fields)
      setFormError(form)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Link to="/maintenance" className="back">
        Maintenance
      </Link>
      <h1 className="page-title">Schedule maintenance</h1>
      <form className="form" onSubmit={(e) => void submit(e)} noValidate>
        <Field id="target" label="Check or group" error={errors.target}>
          <select value={values.target} onChange={(e) => set('target', e.target.value)}>
            <option value="">Choose…</option>
            {!!groups.data?.length && (
              <optgroup label="Groups">
                {groups.data.map((g) => (
                  <option key={g.id} value={`g:${g.id}`}>
                    {g.name}
                  </option>
                ))}
              </optgroup>
            )}
            {!!checks.data?.length && (
              <optgroup label="Checks">
                {[...checks.data]
                  .sort((a, b) => a.name.localeCompare(b.name))
                  .map((c) => (
                    <option key={c.id} value={`c:${c.id}`}>
                      {c.name}
                    </option>
                  ))}
              </optgroup>
            )}
          </select>
        </Field>

        <fieldset className="fieldset">
          <legend>Starts</legend>
          <div className="choice-row">
            <label className="pick">
              <input type="radio" name="start" checked={values.start === 'now'} onChange={() => set('start', 'now')} />
              Now
            </label>
            <label className="pick">
              <input type="radio" name="start" checked={values.start === 'at'} onChange={() => set('start', 'at')} />
              At a set time
            </label>
          </div>
          {values.start === 'at' && (
            <Field id="startsAt" label="Start time" error={errors.startsAt}>
              <input type="datetime-local" value={values.startsAt} onChange={(e) => set('startsAt', e.target.value)} />
            </Field>
          )}
        </fieldset>

        <div className="form-row">
          <Field id="duration" label="Lasts">
            <select value={values.duration} onChange={(e) => set('duration', e.target.value)}>
              {DURATIONS.map((d) => (
                <option key={d.minutes} value={d.minutes}>
                  {d.label}
                </option>
              ))}
              <option value="custom">Until a set time</option>
            </select>
          </Field>
          {values.duration === 'custom' ? (
            <Field id="endsAt" label="End time" error={errors.endsAt}>
              <input type="datetime-local" value={values.endsAt} onChange={(e) => set('endsAt', e.target.value)} />
            </Field>
          ) : (
            errors.endsAt && <p className="field-error form-row-note">{errors.endsAt}</p>
          )}
        </div>

        <Field id="reason" label="Reason" optional error={errors.reason} hint="For your own records.">
          <textarea rows={2} maxLength={500} value={values.reason} onChange={(e) => set('reason', e.target.value)} />
        </Field>

        {formError && (
          <p className="form-error" role="alert">
            {formError}
          </p>
        )}
        <div className="form-actions">
          <button type="submit" className="button button-primary" disabled={busy}>
            {busy ? 'Scheduling…' : 'Schedule maintenance'}
          </button>
          <Link to="/maintenance" className="button">
            Cancel
          </Link>
        </div>
      </form>
    </>
  )
}

export function EditWindow() {
  const id = Number(useParams().id)
  const windows = useMaintenance()
  const found = windows.data?.find((w) => w.id === id)

  if (windows.isPending) return <p className="muted">Loading…</p>
  if (!found) return <NotFound what="maintenance window" />
  return <EditWindowForm window={found} />
}

function EditWindowForm({ window: w }: { window: MaintenanceWindow }) {
  const update = useUpdateWindow()
  const checks = useChecks()
  const groups = useGroups()
  const navigate = useNavigate()
  const [values, setValues] = useState<WindowEditValues>(() => editWindowValues(w))
  const [errors, setErrors] = useState<Partial<Record<keyof WindowEditValues, string>>>({})
  const [formError, setFormError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const now = useNow(15_000)
  const phase = windowPhase(w, now)
  const target = w.check_id
    ? checks.data?.find((c) => c.id === w.check_id)?.name
    : groups.data?.find((g) => g.id === w.group_id)?.name

  async function submit(event: FormEvent) {
    event.preventDefault()
    const { body, errors: found } = buildWindowPatch(values, { ...w, status: phase })
    setErrors(found)
    setFormError(null)
    if (!body) return
    setBusy(true)
    try {
      await update.mutateAsync({ id: w.id, body })
      navigate('/maintenance')
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setFormError(
          phase === 'upcoming'
            ? 'The window has started meanwhile, so its start can no longer change. Reload to edit its end.'
            : 'The window has already ended.',
        )
      } else {
        const { fields, form } = mapErrors(e)
        setErrors(fields)
        setFormError(form)
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Link to="/maintenance" className="back">
        Maintenance
      </Link>
      <h1 className="page-title">Edit maintenance</h1>
      <p className="page-intro">
        {target ?? (w.check_id ? 'Check' : 'Group')}, {windowRange(w)}.
      </p>
      {phase === 'past' ? (
        <p className="muted">This window has ended, so it can't be changed.</p>
      ) : (
        <form className="form" onSubmit={(e) => void submit(e)} noValidate>
          <div className="form-row">
            {phase === 'upcoming' && (
              <Field id="startsAt" label="Starts" error={errors.startsAt}>
                <input
                  type="datetime-local"
                  value={values.startsAt}
                  onChange={(e) => setValues((v) => ({ ...v, startsAt: e.target.value }))}
                />
              </Field>
            )}
            <Field
              id="endsAt"
              label="Ends"
              error={errors.endsAt}
              hint={phase === 'active' ? 'A time in the past ends it now.' : undefined}
            >
              <input
                type="datetime-local"
                value={values.endsAt}
                onChange={(e) => setValues((v) => ({ ...v, endsAt: e.target.value }))}
              />
            </Field>
          </div>
          <Field id="reason" label="Reason" optional error={errors.reason}>
            <textarea
              rows={2}
              maxLength={500}
              value={values.reason}
              onChange={(e) => setValues((v) => ({ ...v, reason: e.target.value }))}
            />
          </Field>
          {formError && (
            <p className="form-error" role="alert">
              {formError}
            </p>
          )}
          <div className="form-actions">
            <button type="submit" className="button button-primary" disabled={busy}>
              {busy ? 'Saving…' : 'Save changes'}
            </button>
            <Link to="/maintenance" className="button">
              Cancel
            </Link>
          </div>
        </form>
      )}
    </>
  )
}
