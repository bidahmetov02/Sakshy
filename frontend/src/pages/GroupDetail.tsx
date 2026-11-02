import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import type { Check, Group } from '../api/client'
import { useChecks, useDeleteGroup, useGroups, useMaintenance, useSetCheckGroup } from '../api/queries'
import { CheckList, Summary } from '../components/CheckList'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { MaintenanceNote } from '../components/MaintenanceNote'
import { errorMessage } from '../lib/errors'
import { plural } from '../lib/format'
import { useNow } from '../lib/useNow'
import { NotFound } from './NotFound'

export function GroupDetail() {
  const id = Number(useParams().id)
  const groups = useGroups()
  const members = useChecks(id)
  const windows = useMaintenance()
  const setGroup = useSetCheckGroup()
  const now = useNow()
  const [adding, setAdding] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const group = groups.data?.find((g) => g.id === id)
  if (groups.isPending) return <p className="muted">Loading…</p>
  if (groups.isError) return <p className="form-error">{errorMessage(groups.error)}</p>
  if (!group) return <NotFound what="group" />

  async function remove(check: Check) {
    setError(null)
    try {
      await setGroup.mutateAsync({ checkIds: [check.id], groupId: null })
    } catch (e) {
      setError(errorMessage(e))
    }
  }

  return (
    <>
      <Link to="/groups" className="back">
        Groups
      </Link>
      <header className="detail-head">
        <div className="detail-title">
          <h1 className="page-title">{group.name}</h1>
          <p className="detail-sub">Alerts go to {group.alert_emails.join(', ')}</p>
        </div>
        <GroupActions group={group} onAdd={() => setAdding(true)} />
      </header>

      <MaintenanceNote windows={windows.data?.filter((w) => w.group_id === id) ?? []} now={now} />

      <section className="section section-tight">
        {members.isPending && <p className="muted">Loading checks…</p>}
        {members.isError && <p className="form-error">{errorMessage(members.error)}</p>}
        {members.data && members.data.length > 0 && (
          <>
            <Summary checks={members.data} small />
            <CheckList
              checks={members.data}
              now={now}
              action={(check) => (
                <button
                  type="button"
                  className="link-button"
                  onClick={() => void remove(check)}
                  disabled={setGroup.isPending}
                  aria-label={`Remove ${check.name} from ${group.name}`}
                >
                  Remove
                </button>
              )}
            />
          </>
        )}
        {members.data?.length === 0 && (
          <div className="empty">
            <p className="muted">No checks in this group yet.</p>
            <button type="button" className="button" onClick={() => setAdding(true)}>
              Add checks
            </button>
          </div>
        )}
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </section>

      {adding && <AddChecksDialog group={group} onClose={() => setAdding(false)} />}
    </>
  )
}

function GroupActions({ group, onAdd }: { group: Group; onAdd: () => void }) {
  const remove = useDeleteGroup(group.id)
  const navigate = useNavigate()
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function del() {
    try {
      await remove.mutateAsync()
      navigate('/groups', { replace: true })
    } catch (e) {
      setConfirming(false)
      setError(errorMessage(e))
    }
  }

  return (
    <div className="actions">
      <div className="actions-row">
        <button type="button" className="button" onClick={onAdd}>
          Add checks
        </button>
        <Link to={`/maintenance/new?target=g:${group.id}`} className="button">
          Schedule maintenance
        </Link>
        <Link to={`/groups/${group.id}/edit`} className="button">
          Edit
        </Link>
        <button type="button" className="button button-quiet-danger" onClick={() => setConfirming(true)}>
          Delete
        </button>
      </div>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <ConfirmDialog
        open={confirming}
        title={`Delete ${group.name}?`}
        confirm="Delete group"
        danger
        busy={remove.isPending}
        onConfirm={() => void del()}
        onClose={() => setConfirming(false)}
      >
        <p>
          {group.check_count
            ? `Its ${plural(group.check_count, 'check')} stay, without a group, and alert their own emails again. `
            : ''}
          Its maintenance windows are deleted, so a down alert they were holding back is sent.
        </p>
      </ConfirmDialog>
    </div>
  )
}

function AddChecksDialog({ group, onClose }: { group: Group; onClose: () => void }) {
  const checks = useChecks()
  const groups = useGroups()
  const setGroup = useSetCheckGroup()
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [error, setError] = useState<string | null>(null)

  const groupName = new Map(groups.data?.map((g) => [g.id, g.name]))
  const candidates = (checks.data ?? [])
    .filter((c) => c.group_id !== group.id)
    .sort((a, b) => a.name.localeCompare(b.name))

  function toggle(id: number) {
    setSelected((s) => {
      const next = new Set(s)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  async function add() {
    setError(null)
    try {
      await setGroup.mutateAsync({ checkIds: [...selected], groupId: group.id })
      onClose()
    } catch (e) {
      setError(errorMessage(e))
    }
  }

  return (
    <ConfirmDialog
      open
      title={`Add checks to ${group.name}`}
      confirm={selected.size ? `Add ${plural(selected.size, 'check')}` : 'Add checks'}
      busy={setGroup.isPending || !selected.size}
      onConfirm={() => void add()}
      onClose={onClose}
    >
      {checks.isPending && <p className="muted">Loading checks…</p>}
      {checks.data && !candidates.length && <p>All your checks are already in this group.</p>}
      {candidates.length > 0 && (
        <>
          <p>Their alerts will go to this group's emails. A check in another group moves here.</p>
          <ul className="pick-list">
            {candidates.map((c) => (
              <li key={c.id}>
                <label className="pick">
                  <input type="checkbox" checked={selected.has(c.id)} onChange={() => toggle(c.id)} />
                  <span>
                    {c.name}
                    {c.group_id && <span className="tag">{groupName.get(c.group_id)}</span>}
                  </span>
                </label>
              </li>
            ))}
          </ul>
        </>
      )}
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
    </ConfirmDialog>
  )
}
