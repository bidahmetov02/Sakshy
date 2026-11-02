import { useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router'
import { ApiError, api, unwrap } from '../api/client'
import { keys, useMe } from '../api/queries'
import { errorMessage, formErrors } from '../lib/errors'

type Mode = 'login' | 'signup'

const COPY: Record<Mode, { title: string; submit: string; busy: string }> = {
  login: { title: 'Sign in', submit: 'Sign in', busy: 'Signing in…' },
  signup: { title: 'Create your account', submit: 'Create account', busy: 'Creating account…' },
}

export function AuthPage({ mode }: { mode: Mode }) {
  const me = useMe()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const from = (useLocation().state as { from?: string } | null)?.from ?? '/'
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<{ email?: string; password?: string; confirm?: string; form?: string }>({})
  const copy = COPY[mode]

  if (me.data) return <Navigate to={from} replace />

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (mode === 'signup' && password !== confirm) {
      setError({ confirm: "Passwords don't match." })
      document.getElementById('confirm')?.focus()
      return
    }
    setBusy(true)
    setError({})
    const body = { email: email.trim(), password }
    try {
      if (mode === 'signup') await unwrap(api.POST('/api/auth/register', { body }))
      else await unwrap(api.POST('/api/auth/login', { body }))
      qc.setQueryData(keys.me, await unwrap(api.GET('/api/auth/me')))
      navigate(from, { replace: true })
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setError({ form: 'Wrong email or password.' })
      else if (e instanceof ApiError && e.status === 409) {
        setError({ email: 'An account with this email already exists. Sign in instead.' })
      } else if (e instanceof ApiError && e.status === 422) {
        const { fields, form } = formErrors(e)
        setError({ email: fields.email, password: fields.password, form: form ?? undefined })
      } else setError({ form: errorMessage(e) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="auth">
      <p className="wordmark">Sakshy</p>
      <h1 className="auth-title">{copy.title}</h1>
      <form className="stack" onSubmit={(e) => void submit(e)}>
        <div className="field">
          <label htmlFor="email">Email</label>
          <input
            id="email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            aria-invalid={!!error.email}
            aria-describedby={error.email ? 'email-error' : undefined}
          />
          {error.email && (
            <p className="field-error" id="email-error">
              {error.email}
            </p>
          )}
        </div>
        <div className="field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            autoComplete={mode === 'signup' ? 'new-password' : 'current-password'}
            required
            minLength={8}
            maxLength={128}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            aria-invalid={!!error.password}
            aria-describedby={mode === 'signup' ? 'password-hint' : undefined}
          />
          {mode === 'signup' && !error.password && (
            <p className="field-hint" id="password-hint">
              At least 8 characters.
            </p>
          )}
          {error.password && <p className="field-error">{error.password}</p>}
        </div>
        {mode === 'signup' && (
          <div className="field">
            <label htmlFor="confirm">Confirm password</label>
            <input
              id="confirm"
              type="password"
              autoComplete="new-password"
              required
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              aria-invalid={!!error.confirm}
              aria-describedby={error.confirm ? 'confirm-error' : undefined}
            />
            {error.confirm && (
              <p className="field-error" id="confirm-error">
                {error.confirm}
              </p>
            )}
          </div>
        )}
        {error.form && (
          <p className="form-error" role="alert">
            {error.form}
          </p>
        )}
        <button type="submit" className="button button-primary" disabled={busy}>
          {busy ? copy.busy : copy.submit}
        </button>
      </form>
      <p className="auth-switch">
        {mode === 'login' ? (
          <>
            New here? <Link to="/signup" state={{ from }}>Create an account</Link>
          </>
        ) : (
          <>
            Already have an account? <Link to="/login" state={{ from }}>Sign in</Link>
          </>
        )}
      </p>
    </main>
  )
}
