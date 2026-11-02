import { Link, useSearchParams } from 'react-router'
import { useChecks, useGroups } from '../api/queries'
import { CheckList, Summary } from '../components/CheckList'
import { errorMessage } from '../lib/errors'
import { useNow } from '../lib/useNow'

export function Dashboard() {
  const [params, setParams] = useSearchParams()
  const groupParam = Number(params.get('group'))
  const groupId = Number.isInteger(groupParam) && groupParam > 0 ? groupParam : undefined
  const checks = useChecks(groupId)
  const groups = useGroups()
  const now = useNow()

  if (checks.isPending) return <p className="muted">Loading checks…</p>
  if (checks.isError) return <p className="form-error">{errorMessage(checks.error)}</p>

  const groupNames = groupId ? undefined : new Map(groups.data?.map((g) => [g.id, g.name]))

  return (
    <>
      <Summary checks={checks.data} />

      <div className="toolbar">
        {!!groups.data?.length && (
          <label className="filter">
            <span className="visually-hidden">Show group</span>
            <select
              value={groupId ?? ''}
              onChange={(e) => setParams(e.target.value ? { group: e.target.value } : {}, { replace: true })}
            >
              <option value="">All checks</option>
              {groups.data.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <Link to="/checks/new" className="button button-primary toolbar-end">
          New check
        </Link>
      </div>

      {checks.data.length > 0 ? (
        <CheckList checks={checks.data} now={now} groupNames={groupNames} />
      ) : (
        groupId && <p className="muted empty">This group has no checks yet.</p>
      )}
    </>
  )
}
