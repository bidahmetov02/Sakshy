import { useState } from 'react'
import { Link } from 'react-router'
import type { MaintenanceWindow } from '../api/client'
import { useChecks, useDeleteWindow, useGroups, useMaintenance, useUpdateWindow } from '../api/queries'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { StateGlyph } from '../components/StateGlyph'
import { errorMessage } from '../lib/errors'
import { windowPhase, windowRange, windowTiming } from '../lib/maintenance'
import { useNow } from '../lib/useNow'

const PAST_SHOWN = 10

export function MaintenancePage() {
  const windows = useMaintenance()
  const checks = useChecks()
  const groups = useGroups()
  const now = useNow(15_000)
  const [allPast, setAllPast] = useState(false)

  const checkName = new Map(checks.data?.map((c) => [c.id, c.name]))
  const groupName = new Map(groups.data?.map((g) => [g.id, g.name]))
  const target = (w: MaintenanceWindow) =>
    w.check_id
      ? { to: `/checks/${w.check_id}`, name: checkName.get(w.check_id) ?? 'Check', kind: 'Check' }
      : { to: `/groups/${w.group_id}`, name: groupName.get(w.group_id ?? 0) ?? 'Group', kind: 'Group' }

  const byPhase = (phase: ReturnType<typeof windowPhase>) =>
    (windows.data ?? []).filter((w) => windowPhase(w, now) === phase)
  const active = byPhase('active').sort((a, b) => Date.parse(a.ends_at) - Date.parse(b.ends_at))
  const upcoming = byPhase('upcoming').sort((a, b) => Date.parse(a.starts_at) - Date.parse(b.starts_at))
  const past = byPhase('past') // newest first, as the API sends them

  const section = (title: string, list: MaintenanceWindow[]) =>
    list.length > 0 && (
      <section className="section section-tight" aria-label={title}>
        <h2 className="section-title">{title}</h2>
        <ul className="rows">
          {list.map((w) => (
            <WindowRow key={w.id} window={w} now={now} target={target(w)} />
          ))}
        </ul>
      </section>
    )

  return (
    <>
      <div className="page-head">
        <h1 className="page-title">Maintenance</h1>
        <Link to="/maintenance/new" className="button button-primary">
          Schedule maintenance
        </Link>
      </div>
      <p className="page-intro">
        During a window, checks keep running but down alerts are held back. If the site is still down when the window
        ends, the alert goes out then.
      </p>

      {windows.isPending && <p className="muted">Loading…</p>}
      {windows.isError && <p className="form-error">{errorMessage(windows.error)}</p>}
      {windows.data?.length === 0 && <p className="muted empty">No maintenance scheduled.</p>}
      {section('Now', active)}
      {section('Upcoming', upcoming)}
      {section('Past', allPast ? past : past.slice(0, PAST_SHOWN))}
      {!allPast && past.length > PAST_SHOWN && (
        <button type="button" className="button load-more" onClick={() => setAllPast(true)}>
          Show all {past.length}
        </button>
      )}
    </>
  )
}

function WindowRow({
  window: w,
  now,
  target,
}: {
  window: MaintenanceWindow
  now: number
  target: { to: string; name: string; kind: string }
}) {
  const update = useUpdateWindow()
  const remove = useDeleteWindow()
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const phase = windowPhase(w, now)

  async function run(action: () => Promise<unknown>) {
    setError(null)
    try {
      await action()
      setConfirming(false)
    } catch (e) {
      setConfirming(false)
      setError(errorMessage(e))
    }
  }

  return (
    <li className={`row window-row is-${phase}`}>
      <div className="window-main">
        <p className="window-target">
          {phase === 'active' && <StateGlyph state="maintenance" />}
          <Link to={target.to}>{target.name}</Link>
          <span className="tag">{target.kind}</span>
        </p>
        <p className="window-when">
          {windowRange(w)} <span className="muted">({windowTiming(w, now)})</span>
        </p>
        {w.reason && <p className="window-reason">{w.reason}</p>}
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </div>
      <div className="actions-row window-actions">
        {phase === 'active' && (
          <button
            type="button"
            className="button"
            disabled={update.isPending}
            // A time in the past ends an active window now, on the server's clock.
            onClick={() => void run(() => update.mutateAsync({ id: w.id, body: { ends_at: new Date().toISOString() } }))}
          >
            End now
          </button>
        )}
        {phase !== 'past' && (
          <Link to={`/maintenance/${w.id}/edit`} className="button">
            Edit
          </Link>
        )}
        <button type="button" className="button button-quiet-danger" onClick={() => setConfirming(true)}>
          Delete
        </button>
      </div>
      <ConfirmDialog
        open={confirming}
        title="Delete this window?"
        confirm="Delete window"
        danger
        busy={remove.isPending}
        onConfirm={() => void run(() => remove.mutateAsync(w.id))}
        onClose={() => setConfirming(false)}
      >
        <p>
          {phase === 'active'
            ? `${target.name} leaves maintenance now. If it's down, the alert it was holding back is sent.`
            : phase === 'upcoming'
              ? 'It will not start.'
              : 'It is removed from the history.'}
        </p>
      </ConfirmDialog>
    </li>
  )
}
