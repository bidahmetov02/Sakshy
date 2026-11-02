import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router'
import { api } from '../api/client'
import { useLiveUpdates } from '../api/live'
import { useHealth, useMe } from '../api/queries'
import { formatDuration } from '../lib/format'
import { Footer } from './Footer'
import { LogoMark } from './Logo'

/** A worker heartbeat older than this means checks aren't running. */
const STALE_WORKER_S = 60

const NAV = [
  ['/', 'Checks'],
  ['/groups', 'Groups'],
  ['/maintenance', 'Maintenance'],
  ['/status-pages', 'Status pages'],
] as const

function Nav() {
  // A check's own pages belong to Checks, which is the index route.
  const onCheck = useLocation().pathname.startsWith('/checks')
  return (
    <nav className="nav" aria-label="Main">
      {NAV.map(([to, label]) => (
        <NavLink
          key={to}
          to={to}
          end={to === '/'}
          className={({ isActive }) => `nav-link${isActive || (to === '/' && onCheck) ? ' active' : ''}`}
        >
          {label}
        </NavLink>
      ))}
    </nav>
  )
}

/** Nothing while live; a quiet note when the stream has been down for a moment. */
function LiveIndicator() {
  const status = useLiveUpdates()
  const [late, setLate] = useState(false)
  useEffect(() => {
    if (status !== 'reconnecting') return
    const timer = setTimeout(() => setLate(true), 3000)
    return () => {
      clearTimeout(timer)
      setLate(false)
    }
  }, [status])
  if (!late) return null
  return (
    <span className="live-note" role="status">
      Reconnecting…
    </span>
  )
}

export function Shell() {
  return (
    <div className="shell">
      <header className="topbar">
        <div className="topbar-inner">
          <Link to="/" className="wordmark">
            <LogoMark className="wordmark-mark" />
            sakshy
          </Link>
          <Nav />
          <LiveIndicator />
          <Account />
        </div>
      </header>
      <MonitorBanner />
      <main className="page">
        <Outlet />
      </main>
      <Footer />
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
