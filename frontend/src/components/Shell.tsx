import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, NavLink, Outlet, useNavigate } from 'react-router'
import { api } from '../api/client'
import { useHealth, useMe } from '../api/queries'
import { formatDuration } from '../lib/format'

/** A worker heartbeat older than this means checks aren't running. */
const STALE_WORKER_S = 60

const YEAR = new Date().getFullYear()

export function Shell() {
  return (
    <div className="shell">
      <header className="topbar">
        <div className="topbar-inner">
          <Link to="/" className="wordmark">
            Sakshy
          </Link>
          <nav className="nav" aria-label="Main">
            <NavLink to="/" end className="nav-link">
              Checks
            </NavLink>
          </nav>
          <Account />
        </div>
      </header>
      <MonitorBanner />
      <main className="page">
        <Outlet />
      </main>
      <footer className="footer">
        <div className="footer-inner">
          <p>© {YEAR}. Developed by Bekzhan Bidakhmetov</p>
          <a href="mailto:bidahmetov2002@gmail.com">bidahmetov2002@gmail.com</a>
        </div>
      </footer>
    </div>
  )
}

function Account() {
  const me = useMe()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [busy, setBusy] = useState(false)

  async function signOut() {
    setBusy(true)
    try {
      await api.POST('/api/auth/logout')
    } finally {
      qc.clear()
      qc.setQueryData(['me'], null)
      navigate('/login', { replace: true })
    }
  }

  return (
    <div className="account">
      <span className="account-email" title={me.data?.email}>
        {me.data?.email}
      </span>
      <button type="button" className="link-button" onClick={() => void signOut()} disabled={busy}>
        Sign out
      </button>
    </div>
  )
}

/** Says so when monitoring itself is down, since every state on screen is then stale. */
function MonitorBanner() {
  const health = useHealth()
  if (!health.data && !health.isError) return null

  let message: string | null = null
  if (health.isError || health.data?.db === 'error') {
    message = "Can't reach the monitoring service. States shown may be out of date."
  } else if (health.data?.worker_last_seen_s == null) {
    message = 'The checker isn’t running, so no checks are being made.'
  } else if (health.data.worker_last_seen_s > STALE_WORKER_S) {
    message = `The checker last reported ${formatDuration(health.data.worker_last_seen_s * 1000)} ago. No checks are being made, and states shown may be out of date.`
  }
  if (!message) return null
  return (
    <div className="banner" role="status">
      <p className="banner-inner">{message}</p>
    </div>
  )
}
