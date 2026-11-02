/** Display formatting. Dates use the browser's locale and time zone unless given. */

const MINUTE = 60
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

/** An elapsed time at the coarsest useful precision: "45 s", "12 min", "3 h 20 min", "4 d 2 h". */
export function formatDuration(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  if (s < MINUTE) return `${s} s`
  if (s < HOUR) return `${Math.floor(s / MINUTE)} min`
  if (s < DAY) {
    const h = Math.floor(s / HOUR)
    const m = Math.floor((s % HOUR) / MINUTE)
    return m ? `${h} h ${m} min` : `${h} h`
  }
  const d = Math.floor(s / DAY)
  const h = Math.floor((s % DAY) / HOUR)
  return h ? `${d} d ${h} h` : `${d} d`
}

/** A configured interval, exact: "30 s", "1 min", "1 min 30 s", "1 h". */
export function formatInterval(seconds: number): string {
  const parts: string[] = []
  const h = Math.floor(seconds / HOUR)
  const m = Math.floor((seconds % HOUR) / MINUTE)
  const s = seconds % MINUTE
  if (h) parts.push(`${h} h`)
  if (m) parts.push(`${m} min`)
  if (s || !parts.length) parts.push(`${s} s`)
  return parts.join(' ')
}

/** A response time: "84 ms", "1.24 s", "12.3 s". */
export function formatMs(ms: number | null | undefined): string {
  if (ms == null) return '–'
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`
}

/** Uptime, never rounded up to 100%: 99.996 reads "99.99%". */
export function formatUptime(pct: number | null | undefined): string | null {
  if (pct == null) return null
  if (pct >= 100) return '100%'
  return `${(Math.floor(pct * 100) / 100).toFixed(2)}%`
}

function sameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  )
}

/** "Today 14:05", "Yesterday 09:12", "Nov 1, 14:05", "Dec 30, 2025, 14:05". */
export function formatWhen(iso: string, now: Date = new Date(), locale?: string): string {
  const date = new Date(iso)
  const time = date.toLocaleTimeString(locale, { timeStyle: 'short' })
  if (sameDay(date, now)) return `Today ${time}`
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (sameDay(date, yesterday)) return `Yesterday ${time}`
  const day = date.toLocaleDateString(locale, {
    month: 'short',
    day: 'numeric',
    year: date.getFullYear() === now.getFullYear() ? undefined : 'numeric',
  })
  return `${day}, ${time}`
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`
}
