import { Link } from 'react-router'
import { useGroups } from '../api/queries'
import { StateGlyph } from '../components/StateGlyph'
import { errorMessage } from '../lib/errors'
import { STATUS_GLYPH, STATUS_LABEL, groupCounts } from '../lib/groups'

export function GroupsPage() {
  const groups = useGroups()

  return (
    <>
      <div className="page-head">
        <h1 className="page-title">Groups</h1>
        <Link to="/groups/new" className="button button-primary">
          New group
        </Link>
      </div>
      <p className="page-intro">
        Checks in a group share its alert emails and its maintenance windows, and you can filter the dashboard by
        group.
      </p>

      {groups.isPending && <p className="muted">Loading groups…</p>}
      {groups.isError && <p className="form-error">{errorMessage(groups.error)}</p>}
      {groups.data?.length === 0 && <p className="muted empty">No groups yet.</p>}
      {!!groups.data?.length && (
        <ul className="rows">
          {groups.data.map((g) => (
            <li key={g.id} className={`row row-${STATUS_GLYPH[g.status]}`}>
              <Link to={`/groups/${g.id}`} className="row-link row-grid-group">
                <StateGlyph state={STATUS_GLYPH[g.status]} />
                <span className="row-name">{g.name}</span>
                <span className="row-meta">{groupCounts(g)}</span>
                <span className="row-status">{STATUS_LABEL[g.status]}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </>
  )
}
