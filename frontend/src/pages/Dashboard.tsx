import { Link, useSearchParams } from 'react-router'
import type { Check } from '../api/client'
import { useChecks, useGroups } from '../api/queries'
import { StateGlyph } from '../components/StateGlyph'
import { displayState, sortChecks, stateSince, summarize } from '../lib/checks'
import { errorMessage } from '../lib/errors'
import { formatMs } from '../lib/format'
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

  const summary = summarize(checks.data)
  const sorted = sortChecks(checks.data)
  const groupName = new Map(groups.data?.map((g) => [g.id, g.name]))

  return (
    <>
      <section className="summary" aria-live="polite">
        <h1 className={`summary-headline tone-${summary.tone}`}>{summary.headline}</h1>
        {summary.detail && <p className="summary-detail">{summary.detail}</p>}
      </section>

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

      {sorted.length > 0 ? (
        <ul className="check-list">
          {sorted.map((check) => (
            <CheckRow
              key={check.id}
              check={check}
              now={now}
              group={!groupId && check.group_id ? groupName.get(check.group_id) : undefined}
            />
          ))}
        </ul>
      ) : (
        groupId && <p className="muted empty">This group has no checks yet.</p>
      )}
    </>
  )
}

function CheckRow({ check, now, group }: { check: Check; now: number; group?: string }) {
  const state = displayState(check)
  const showMs = (state === 'up' || state === 'failing') && check.last_response_ms != null
  return (
    <li className={`check-row row-${state}`}>
      <Link to={`/checks/${check.id}`} className="check-link">
        <StateGlyph state={state} />
        <span className="check-name">
          {check.name}
          {check.in_maintenance && <span className="tag tag-maint">Maintenance</span>}
          {group && <span className="tag">{group}</span>}
        </span>
        <span className="check-url">{check.url}</span>
        <span className="check-ms">{showMs ? formatMs(check.last_response_ms) : ''}</span>
        <span className="check-since">{stateSince(check, now)}</span>
      </Link>
    </li>
  )
}
