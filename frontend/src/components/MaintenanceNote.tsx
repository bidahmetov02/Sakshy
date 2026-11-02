import { Link } from 'react-router'
import type { MaintenanceWindow } from '../api/client'
import { formatWhen } from '../lib/format'
import { windowPhase, windowRange } from '../lib/maintenance'

/** "16:00" today, else "Nov 3, 02:00". */
function until(iso: string): string {
  const end = new Date(iso)
  return end.toDateString() === new Date().toDateString()
    ? end.toLocaleTimeString(undefined, { timeStyle: 'short' })
    : formatWhen(iso)
}

/** The current and upcoming windows that cover a check or group, as short notes. */
export function MaintenanceNote({ windows, now }: { windows: readonly MaintenanceWindow[]; now: number }) {
  const shown = windows
    .filter((w) => windowPhase(w, now) !== 'past')
    .sort((a, b) => Date.parse(a.starts_at) - Date.parse(b.starts_at))
  if (!shown.length) return null
  return (
    <ul className="maint-notes">
      {shown.map((w) => {
        const active = windowPhase(w, now) === 'active'
        return (
          <li key={w.id} className="maint-note">
            {active ? (
              <>
                <strong>In maintenance</strong> until {until(w.ends_at)}. Down alerts are held back until then.
              </>
            ) : (
              <>
                <strong>Maintenance scheduled</strong> {windowRange(w)}.
              </>
            )}
            {w.reason && <span className="muted"> {w.reason}</span>}{' '}
            <Link to="/maintenance">Manage</Link>
          </li>
        )
      })}
    </ul>
  )
}
