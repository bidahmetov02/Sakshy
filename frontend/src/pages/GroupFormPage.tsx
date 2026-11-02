import { useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { ApiError, type Group, type GroupIn } from '../api/client'
import { useCreateGroup, useGroups, useMe, useUpdateGroup } from '../api/queries'
import { Field } from '../components/Field'
import { parseEmails } from '../lib/checkForm'
import { formErrors } from '../lib/errors'
import { validateGroup, type GroupFormValues } from '../lib/groups'
import { NotFound } from './NotFound'

export function NewGroup() {
  const create = useCreateGroup()
  const navigate = useNavigate()
  return (
    <>
      <Link to="/groups" className="back">
        Groups
      </Link>
      <h1 className="page-title">New group</h1>
      <GroupForm
        submit={{ idle: 'Create group', busy: 'Creating…' }}
        onSubmit={async (body) => {
          const group = await create.mutateAsync(body)
          navigate(`/groups/${group.id}`, { replace: true })
        }}
      />
    </>
  )
}

export function EditGroup() {
  const id = Number(useParams().id)
  const groups = useGroups()
  const update = useUpdateGroup(id)
  const navigate = useNavigate()
  const group = groups.data?.find((g) => g.id === id)

  if (groups.isPending) return <p className="muted">Loading…</p>
  if (!group) return <NotFound what="group" />
  return (
    <>
      <Link to={`/groups/${id}`} className="back">
        {group.name}
      </Link>
      <h1 className="page-title">Edit group</h1>
      <GroupForm
        group={group}
        submit={{ idle: 'Save changes', busy: 'Saving…' }}
        onSubmit={async (body) => {
          await update.mutateAsync(body)
          navigate(`/groups/${id}`)
        }}
      />
    </>
  )
}

function GroupForm({
  group,
  submit,
  onSubmit,
}: {
  group?: Group
  submit: { idle: string; busy: string }
  onSubmit: (body: GroupIn) => Promise<void>
}) {
  const accountEmail = useMe().data?.email ?? ''
  const [values, setValues] = useState<GroupFormValues>({
    name: group?.name ?? '',
    alert_emails: (group?.alert_emails ?? [accountEmail]).join('\n'),
  })
  const [errors, setErrors] = useState<Partial<Record<keyof GroupFormValues, string>>>({})
  const [formError, setFormError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    const found = validateGroup(values)
    setErrors(found)
    setFormError(null)
    if (Object.keys(found).length) {
      document.getElementById(`f-${Object.keys(found)[0]}`)?.focus()
      return
    }
    setBusy(true)
    try {
      await onSubmit({ name: values.name.trim(), alert_emails: parseEmails(values.alert_emails) })
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setErrors({ name: 'You already have a group with this name.' })
      } else {
        const { fields, form } = formErrors(e)
        setErrors(fields)
        setFormError(form)
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="form" onSubmit={(e) => void handleSubmit(e)} noValidate>
      <Field id="name" label="Name" error={errors.name}>
        <input
          value={values.name}
          onChange={(e) => setValues((v) => ({ ...v, name: e.target.value }))}
          maxLength={200}
          autoFocus={!group}
        />
      </Field>
      <Field
        id="alert_emails"
        label="Alert emails"
        error={errors.alert_emails}
        hint="One per line. These replace the own alert emails of every check in the group."
      >
        <textarea
          rows={3}
          value={values.alert_emails}
          onChange={(e) => setValues((v) => ({ ...v, alert_emails: e.target.value }))}
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
        <Link to={group ? `/groups/${group.id}` : '/groups'} className="button">
          Cancel
        </Link>
      </div>
    </form>
  )
}
