import { useState, type FormEvent } from 'react'
import { Field } from '../components/Field'
import { Link, useNavigate, useParams } from 'react-router'
import type { Check, CheckIn, Group } from '../api/client'
import { useCheck, useCreateCheck, useGroups, useMe, useUpdateCheck } from '../api/queries'
import {
  MAX_TIMEOUT_S,
  initialValues,
  intervalOptions,
  normalizeUrl,
  toPayload,
  validate,
  type CheckFormErrors,
  type CheckFormValues,
} from '../lib/checkForm'
import { formErrors } from '../lib/errors'
import { NotFound } from './NotFound'

export function NewCheck() {
  const create = useCreateCheck()
  const navigate = useNavigate()
  return (
    <>
      <Link to="/" className="back">
        Checks
      </Link>
      <h1 className="page-title">New check</h1>
      <CheckForm
        submit={{ idle: 'Create check', busy: 'Creating…' }}
        onSubmit={async (body) => {
          const check = await create.mutateAsync(body)
          navigate(`/checks/${check.id}`, { replace: true })
        }}
      />
    </>
  )
}

export function EditCheck() {
  const id = Number(useParams().id)
  const check = useCheck(id)
  const update = useUpdateCheck(id)
  const navigate = useNavigate()

  if (check.isPending) return <p className="muted">Loading…</p>
  if (check.isError) return <NotFound what="check" />
  return (
    <>
      <Link to={`/checks/${id}`} className="back">
        {check.data.name}
      </Link>
      <h1 className="page-title">Edit check</h1>
      {check.data.state === 'down' && !check.data.is_paused && (
        <p className="note">
          This check is down. It stays down until a run with the new settings succeeds, and then you get
          the recovered email.
        </p>
      )}
      <CheckForm
        check={check.data}
        submit={{ idle: 'Save changes', busy: 'Saving…' }}
        onSubmit={async (body) => {
          await update.mutateAsync(body)
          navigate(`/checks/${id}`)
        }}
      />
    </>
  )
}

function CheckForm({
  check,
  submit,
  onSubmit,
}: {
  check?: Check
  submit: { idle: string; busy: string }
  onSubmit: (body: CheckIn) => Promise<void>
}) {
  const accountEmail = useMe().data?.email ?? ''
  const groups = useGroups()
  const [values, setValues] = useState<CheckFormValues>(() => initialValues(check, accountEmail))
  const [errors, setErrors] = useState<CheckFormErrors>({})
  const [formError, setFormError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const set = (field: keyof CheckFormValues) => (value: string) => setValues((v) => ({ ...v, [field]: value }))
  const group = groups.data?.find((g) => String(g.id) === values.group_id)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    const found = validate(values)
    setErrors(found)
    setFormError(null)
    if (Object.keys(found).length) {
      document.getElementById(`f-${Object.keys(found)[0]}`)?.focus()
      return
    }
    setBusy(true)
    try {
      await onSubmit(toPayload(values, accountEmail))
    } catch (e) {
      const { fields, form } = formErrors(e)
      setErrors(fields as CheckFormErrors)
      setFormError(form)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="form" onSubmit={(e) => void handleSubmit(e)} noValidate>
      <Field id="name" label="Name" error={errors.name}>
        <input value={values.name} onChange={(e) => set('name')(e.target.value)} maxLength={200} autoFocus={!check} />
      </Field>
      <Field id="url" label="URL" error={errors.url}>
        <input
          type="url"
          inputMode="url"
          placeholder="https://example.com/health"
          value={values.url}
          onChange={(e) => set('url')(e.target.value)}
          onBlur={(e) => set('url')(normalizeUrl(e.target.value))}
        />
      </Field>

      <div className="form-row">
        <Field id="interval_s" label="Check every">
          <select value={values.interval_s} onChange={(e) => set('interval_s')(e.target.value)}>
            {intervalOptions(check?.interval_s).map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>
        <Field
          id="timeout_s"
          label="Timeout"
          unit="seconds"
          error={errors.timeout_s}
          hint={`Up to ${MAX_TIMEOUT_S} s, and shorter than the interval.`}
        >
          <input
            type="number"
            inputMode="decimal"
            min={0.01}
            max={MAX_TIMEOUT_S}
            step="any"
            value={values.timeout_s}
            onChange={(e) => set('timeout_s')(e.target.value)}
          />
        </Field>
      </div>

      <div className="form-row">
        <Field id="expected_status" label="Expected status" error={errors.expected_status}>
          <input
            type="number"
            inputMode="numeric"
            min={100}
            max={599}
            value={values.expected_status}
            onChange={(e) => set('expected_status')(e.target.value)}
          />
        </Field>
        <Field
          id="expected_body"
          label="Expected text"
          optional
          error={errors.expected_body}
          hint="The check fails if the response doesn't contain it."
        >
          <input value={values.expected_body} onChange={(e) => set('expected_body')(e.target.value)} />
        </Field>
      </div>

      {!!groups.data?.length && (
        <Field id="group_id" label="Group" error={errors.group_id}>
          <select value={values.group_id} onChange={(e) => set('group_id')(e.target.value)}>
            <option value="">No group</option>
            {groups.data.map((g) => (
              <option key={g.id} value={g.id}>
                {g.name}
              </option>
            ))}
          </select>
        </Field>
      )}

      <Field
        id="alert_emails"
        label="Alert emails"
        error={errors.alert_emails}
        hint={group ? <GroupRecipients group={group} /> : 'One per line. They get an email when the site goes down and when it recovers.'}
      >
        <textarea
          rows={3}
          value={values.alert_emails}
          onChange={(e) => set('alert_emails')(e.target.value)}
          disabled={!!group}
          spellCheck={false}
        />
      </Field>

      {formError && (
        <p className="form-error" role="alert">
          {formError}
        </p>
      )}
      <div className="form-actions">
        <button type="submit" className="button button-primary" disabled={busy}>
          {busy ? submit.busy : submit.idle}
        </button>
        <Link to={check ? `/checks/${check.id}` : '/'} className="button">
          Cancel
        </Link>
      </div>
    </form>
  )
}

function GroupRecipients({ group }: { group: Group }) {
  return (
    <>
      Alerts go to group <strong>{group.name}</strong>: {group.alert_emails.join(', ')}. This check's emails are
      used only when it isn't in a group.
    </>
  )
}
