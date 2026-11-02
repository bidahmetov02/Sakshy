import { useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'
import { ApiError, type Check, type Range } from '../api/client'
import {
  useCheck,
  useCheckAction,
  useDeleteCheck,
  useGroups,
  useIncidents,
  useMaintenance,
  useResults,
  useStats,
  type CheckAction,
} from '../api/queries'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { MaintenanceNote } from '../components/MaintenanceNote'
import { ResponseChart } from '../components/ResponseChart'
import { StateGlyph } from '../components/StateGlyph'
import { averageMs, toSlots } from '../lib/chart'
import { END_REASON_LABEL, describeError, displayState, statusSentence } from '../lib/checks'
import { errorMessage } from '../lib/errors'
import { formatDuration, formatInterval, formatMs, formatUptime, formatWhen } from '../lib/format'
import { useNow } from '../lib/useNow'
import { NotFound } from './NotFound'

const RANGES: { value: Range; label: string }[] = [
  { value: 'day', label: '24 hours' },
  { value: 'week', label: '7 days' },
  { value: 'month', label: '30 days' },
]

export function CheckDetail() {
  const id = Number(useParams().id)
  const now = useNow(1000)
  const check = useCheck(id)

  if (!Number.isInteger(id)) return <NotFound what="check" />
  if (check.isPending) return <p className="muted">Loading…</p>
  if (check.isError) {
    if (check.error instanceof ApiError && check.error.status === 404) return <NotFound what="check" />
    return <p className="form-error">{errorMessage(check.error)}</p>
  }
  const c = check.data

  return (
    <>
      <Link to="/" className="back">
        Checks
      </Link>
      <header className="detail-head">
        <div className="detail-title">
          <h1 className="page-title">{c.name}</h1>
          <a className="detail-url" href={c.url} target="_blank" rel="noreferrer noopener">
            {c.url}
          </a>
        </div>
        <Actions check={c} />
      </header>

      <section className={`status-line row-${displayState(c)}`} aria-live="polite">
        <StateGlyph state={displayState(c)} />
        <p>
          {statusSentence(c, now)}
          {!c.is_paused && c.state !== 'pending' && <NextRun at={c.next_run_at} now={now} />}
        </p>
      </section>
      <CheckMaintenance check={c} now={now} />

      <History check={c} />
      <Incidents checkId={c.id} now={now} />
      <Results checkId={c.id} />
      <Settings check={c} />
    </>
  )
}

function NextRun({ at, now }: { at: string; now: number }) {
  const ms = Date.parse(at) - now
  return <span className="muted"> {ms <= 0 ? 'Checking now…' : `Next check in ${formatDuration(ms)}.`}</span>
}

function Actions({ check }: { check: Check }) {
  const action = useCheckAction(check.id)
  const remove = useDeleteCheck(check.id)
  const navigate = useNavigate()
  const [confirm, setConfirm] = useState<'pause' | 'delete' | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  async function run(kind: CheckAction) {
    setMessage(null)
    try {
      await action.mutateAsync(kind)
      setConfirm(null)
    } catch (e) {
      setMessage(
        kind === 'run' && e instanceof ApiError && e.status === 409
          ? 'A check is already running. Its result shows up here when it finishes.'
          : errorMessage(e),
      )
    }
  }

  async function del() {
    try {
      await remove.mutateAsync()
      navigate('/', { replace: true })
    } catch (e) {
      setConfirm(null)
      setMessage(errorMessage(e))
    }
  }

  const down = check.state === 'down' && !check.is_paused
  return (
    <div className="actions">
      <div className="actions-row">
        {!check.is_paused && (
          <button type="button" className="button" onClick={() => void run('run')} disabled={action.isPending}>
            Run now
          </button>
        )}
        {check.is_paused ? (
          <button type="button" className="button button-primary" onClick={() => void run('resume')} disabled={action.isPending}>
            Resume
          </button>
        ) : (
          <button
            type="button"
            className="button"
            onClick={() => (down ? setConfirm('pause') : void run('pause'))}
            disabled={action.isPending}
          >
            Pause
          </button>
        )}
        <Link to={`/checks/${check.id}/edit`} className="button">
          Edit
        </Link>
        <button type="button" className="button button-quiet-danger" onClick={() => setConfirm('delete')}>
          Delete
        </button>
      </div>
      {message && (
        <p className="form-error" role="alert">
          {message}
        </p>
      )}

      <ConfirmDialog
        open={confirm === 'pause'}
        title="Pause while it's down?"
        confirm="Pause check"
        busy={action.isPending}
        onConfirm={() => void run('pause')}
        onClose={() => setConfirm(null)}
      >
        <p>Pausing ends the open incident, so you won't get a recovered email for it. Resuming starts fresh.</p>
      </ConfirmDialog>
      <ConfirmDialog
        open={confirm === 'delete'}
        title={`Delete ${check.name}?`}
        confirm="Delete check"
        danger
        busy={remove.isPending}
        onConfirm={() => void del()}
        onClose={() => setConfirm(null)}
      >
        <p>Its history and incidents go with it. This can't be undone.</p>
      </ConfirmDialog>
    </div>
  )
}

function History({ check }: { check: Check }) {
  const [params, setParams] = useSearchParams()
  const range: Range = (['day', 'week', 'month'] as const).find((r) => r === params.get('range')) ?? 'day'
  const stats = useStats(check.id, range)

  const uptime = formatUptime(stats.data?.uptime_pct)
  const avg = stats.data ? averageMs(toSlots(stats.data.buckets)) : null

  return (
    <section className="section" aria-labelledby="history-title">
      <div className="section-head">
        <h2 id="history-title" className="section-title">
          Response time
        </h2>
        <div className="segmented" role="group" aria-label="Period">
          {RANGES.map((r) => (
            <button
              key={r.value}
              type="button"
              aria-pressed={r.value === range}
              onClick={() => setParams(r.value === 'day' ? {} : { range: r.value }, { replace: true })}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>
      {stats.data && (
        <p className="figures">
          <span>
            <strong className="figure">{uptime ?? '–'}</strong> uptime
          </span>
          <span>
            <strong className="figure">{formatMs(avg)}</strong> average
          </span>
        </p>
      )}
      {stats.isError && <p className="form-error">{errorMessage(stats.error)}</p>}
      {stats.data && <ResponseChart stats={stats.data} range={range} stale={stats.isPlaceholderData} />}
      {stats.data && (
        <p className="legend">
          <span className="legend-item">
            <span className="tip-key strip-ok" aria-hidden="true" /> All passed
          </span>
          <span className="legend-item">
            <span className="tip-key strip-partial" aria-hidden="true" /> Some failed
          </span>
          <span className="legend-item">
            <span className="tip-key strip-fail" aria-hidden="true" /> All failed
          </span>
          <span className="legend-item">
            <span className="tip-key strip-none" aria-hidden="true" /> No runs
          </span>
        </p>
      )}
    </section>
  )
}

function Incidents({ checkId, now }: { checkId: number; now: number }) {
  const incidents = useIncidents(checkId)
  const items = incidents.data?.pages.flatMap((p) => p.items) ?? []

  return (
    <section className="section" aria-labelledby="incidents-title">
      <h2 id="incidents-title" className="section-title">
        Incidents
      </h2>
      {incidents.isError && <p className="form-error">{errorMessage(incidents.error)}</p>}
      {incidents.isSuccess && !items.length && <p className="muted">No incidents. This site hasn't been down.</p>}
      {items.length > 0 && (
        <ul className="log">
          {items.map((i) => {
            const open = i.ended_at === null
            const duration = open ? now - Date.parse(i.started_at) : (i.duration_s ?? 0) * 1000
            return (
              <li key={i.id} className={`log-row${open ? ' is-open' : ''}`}>
                <span className="log-when">{formatWhen(i.started_at)}</span>
                <span className="log-what">
                  {describeError(i.cause_kind, i.cause_message) ?? 'Down'}
                  {i.began_in_maintenance && !i.alerted && <span className="tag tag-maint">No alert: maintenance</span>}
                </span>
                <span className="log-dur">{open ? `Ongoing, ${formatDuration(duration)}` : formatDuration(duration)}</span>
                <span className="log-end">{i.end_reason ? END_REASON_LABEL[i.end_reason] : ''}</span>
              </li>
            )
          })}
        </ul>
      )}
      <LoadMore query={incidents} />
    </section>
  )
}

function Results({ checkId }: { checkId: number }) {
  const results = useResults(checkId)
  const items = results.data?.pages.flatMap((p) => p.items) ?? []

  return (
    <section className="section" aria-labelledby="results-title">
      <h2 id="results-title" className="section-title">
        Recent checks
      </h2>
      {results.isError && <p className="form-error">{errorMessage(results.error)}</p>}
      {results.isSuccess && !items.length && <p className="muted">No checks have run yet.</p>}
      {items.length > 0 && (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Time</th>
                <th scope="col">Result</th>
                <th scope="col" className="num">
                  Response
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((r) => (
                <tr key={`${r.checked_at}-${r.is_retry}`} className={r.ok ? undefined : 'is-failed'}>
                  <td className="nowrap">{formatWhen(r.checked_at, undefined, undefined, 'medium')}</td>
                  <td>
                    <span className="result">
                      <StateGlyph state={r.ok ? 'up' : 'down'} />
                      <span>
                        {r.ok ? `Passed, status ${r.status_code}` : describeError(r.error_kind, null, r.status_code)}
                        {(r.is_retry || r.in_maintenance) && (
                          <span className="muted">
                            {' '}
                            ({[r.is_retry && 'retry', r.in_maintenance && 'maintenance'].filter(Boolean).join(', ')})
                          </span>
                        )}
                        {!r.ok && r.error_message && r.error_kind !== 'status_mismatch' && (
                          <span className="result-msg">{r.error_message}</span>
                        )}
                      </span>
                    </span>
                  </td>
                  <td className="num">{formatMs(r.response_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <LoadMore query={results} />
    </section>
  )
}

function LoadMore({
  query,
}: {
  query: { hasNextPage: boolean; isFetchingNextPage: boolean; fetchNextPage: () => Promise<unknown> }
}) {
  if (!query.hasNextPage) return null
  return (
    <button
      type="button"
      className="button load-more"
      onClick={() => void query.fetchNextPage()}
      disabled={query.isFetchingNextPage}
    >
      {query.isFetchingNextPage ? 'Loading…' : 'Show older'}
    </button>
  )
}

function Settings({ check }: { check: Check }) {
  const groups = useGroups()
  const group = groups.data?.find((g) => g.id === check.group_id)
  return (
    <section className="section" aria-labelledby="settings-title">
      <h2 id="settings-title" className="section-title">
        Settings
      </h2>
      <dl className="settings">
        <dt>Checked every</dt>
        <dd>{formatInterval(check.interval_s)}</dd>
        <dt>Timeout</dt>
        <dd>{check.timeout_s} s</dd>
        <dt>Expected status</dt>
        <dd>{check.expected_status}</dd>
        <dt>Expected text</dt>
        <dd>{check.expected_body ? <q>{check.expected_body}</q> : <span className="muted">None</span>}</dd>
        {check.group_id && (
          <>
            <dt>Group</dt>
            <dd>
              <Link to={`/groups/${check.group_id}`}>{group?.name ?? 'Group'}</Link>
            </dd>
          </>
        )}
        <dt>Alerts go to</dt>
        <dd>
          {check.effective_alert_emails.join(', ')}
          {check.group_id && <span className="muted"> (the group's list)</span>}
        </dd>
        <dt>Maintenance</dt>
        <dd>
          <Link to={`/maintenance/new?target=c:${check.id}`}>Schedule maintenance</Link>
        </dd>
      </dl>
    </section>
  )
}

/** Windows on the check itself or on its group. */
function CheckMaintenance({ check, now }: { check: Check; now: number }) {
  const windows = useMaintenance()
  const covering =
    windows.data?.filter((w) => w.check_id === check.id || (check.group_id && w.group_id === check.group_id)) ?? []
  return <MaintenanceNote windows={covering} now={now} />
}
