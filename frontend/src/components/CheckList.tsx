import type { ReactNode } from 'react'
import { Link } from 'react-router'
import type { Check } from '../api/client'
import { displayState, sortChecks, stateSince, summarize } from '../lib/checks'
import { formatMs } from '../lib/format'
import { StateGlyph } from './StateGlyph'

/** The large opening sentence: is anything down. `small` for a page that has its own title. */
export function Summary({ checks, small }: { checks: readonly Check[]; small?: boolean }) {
  const summary = summarize(checks)
  const Headline = small ? 'p' : 'h1'
  return (
    <section className={`summary${small ? ' summary-small' : ''}`} aria-live="polite">
      <Headline className={`summary-headline tone-${summary.tone}`}>{summary.headline}</Headline>
      {summary.detail && <p className="summary-detail">{summary.detail}</p>}
    </section>
  )
}

/** Checks as rows, worst first. `action` puts a control at the end of each row. */
export function CheckList({
  checks,
  now,
  groupNames,
  action,
}: {
  checks: readonly Check[]
  now: number
  /** Tag each row with its group's name. */
  groupNames?: Map<number, string>
  action?: (check: Check) => ReactNode
}) {
  return (
    <ul className="check-list">
      {sortChecks(checks).map((check) => (
        <CheckRow
          key={check.id}
          check={check}
          now={now}
          group={check.group_id ? groupNames?.get(check.group_id) : undefined}
          action={action?.(check)}
        />
      ))}
    </ul>
  )
}

function CheckRow({ check, now, group, action }: { check: Check; now: number; group?: string; action?: ReactNode }) {
  const state = displayState(check)
  const showMs = (state === 'up' || state === 'failing') && check.last_response_ms != null
  return (
    <li className={`check-row row-${state}${action ? ' has-action' : ''}`}>
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
      {action && <div className="check-action">{action}</div>}
    </li>
  )
}
