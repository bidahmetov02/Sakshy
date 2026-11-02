import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useState, type KeyboardEvent, type PointerEvent } from 'react'
import { useParams } from 'react-router'
import { ApiError, type PublicItem, type PublicPage } from '../api/client'
import { keys, usePublicPage, usePublicStats } from '../api/queries'
import { Footer } from '../components/Footer'
import { StateGlyph } from '../components/StateGlyph'
import { slotAt, slotStatus } from '../lib/chart'
import { errorMessage } from '../lib/errors'
import { formatDuration, formatMs, formatUptime, formatWhen, plural } from '../lib/format'
import { OVERALL, changedItems, dailyUptime, publicStateText, type Day } from '../lib/statusPages'
import { useNow } from '../lib/useNow'

/** A stream the browser gave up on is reopened after this long. */
const REOPEN_MS = 10_000

/** Keeps the page live: `page` events carry the whole page; `ready` (every reconnect) refetches. */
function usePublicLive(slug: string, enabled: boolean) {
  const qc = useQueryClient()
  useEffect(() => {
    if (!enabled) return
    let source: EventSource | null = null
    let reopen: ReturnType<typeof setTimeout> | undefined
    function open() {
      source = new EventSource(`/api/public/${encodeURIComponent(slug)}/events`)
      source.addEventListener('ready', () => void qc.invalidateQueries({ queryKey: keys.publicPage(slug) }))
      source.addEventListener('page', (e) => {
        const next = JSON.parse(e.data) as PublicPage
        const before = qc.getQueryData<PublicPage>(keys.publicPage(slug))
        qc.setQueryData(keys.publicPage(slug), next)
        // Today's bar moves when an item's state does; otherwise it can wait for its refetch.
        for (const id of changedItems(before, next)) {
          void qc.invalidateQueries({ queryKey: keys.publicStats(slug, id) })
        }
      })
      source.onerror = () => {
        if (source?.readyState !== EventSource.CLOSED) return
        // Unpublished or deleted pages answer 404: the refetch shows that, and stops the stream.
        void qc.invalidateQueries({ queryKey: keys.publicPage(slug) })
        reopen = setTimeout(open, REOPEN_MS)
      }
    }
    open()
    return () => {
      clearTimeout(reopen)
      source?.close()
    }
  }, [slug, enabled, qc])
}

export function PublicStatusPage() {
  const slug = useParams().slug ?? ''
  const page = usePublicPage(slug)
  const now = useNow(15_000)
  usePublicLive(slug, page.isSuccess)

  const title = page.data?.title
  useEffect(() => {
    document.title = title ? `${title} status` : 'Status'
    return () => {
      document.title = 'Sakshy'
    }
  }, [title])

  return (
    <div className="shell public">
      <main className="page public-page">
        {page.isPending && <p className="muted">Loading…</p>}
        {page.isError &&
          (page.error instanceof ApiError && page.error.status === 404 ? (
            <section className="not-found">
              <h1 className="page-title">This status page isn't available</h1>
              <p className="muted">It may have been unpublished, or the link is wrong.</p>
            </section>
          ) : (
            <p className="form-error">{errorMessage(page.error)}</p>
          ))}
        {page.data && <PageBody slug={slug} page={page.data} now={now} />}
      </main>
      <Footer />
    </div>
  )
}

function overallDetail(items: readonly PublicItem[]): string | null {
  const counted = items.filter((i) => i.state !== 'paused' && i.state !== 'maintenance')
  const down = counted.filter((i) => i.state === 'down').length
  const maintenance = items.filter((i) => i.state === 'maintenance').length
  const parts: string[] = []
  if (down) parts.push(`${down} of ${plural(counted.length, 'service')} ${down === 1 ? 'is' : 'are'} down.`)
  if (maintenance) parts.push(`${maintenance} ${maintenance === 1 ? 'is' : 'are'} under maintenance.`)
  return parts.join(' ') || null
}

function PageBody({ slug, page, now }: { slug: string; page: PublicPage; now: number }) {
  const tone = page.overall_status === 'operational' ? 'neutral' : 'down'
  const detail = overallDetail(page.items)
  const showUptime = page.items.some((i) => i.uptime_pct !== undefined)

  return (
    <>
      <p className="public-title">{page.title}</p>
      <section className="summary" aria-live="polite">
        <h1 className={`summary-headline tone-${tone}`}>{OVERALL[page.overall_status]}</h1>
        {detail && <p className="summary-detail">{detail}</p>}
      </section>

      {page.items.length === 0 ? (
        <p className="muted">Nothing is listed on this page yet.</p>
      ) : (
        <ul className="public-items">
          {page.items.map((item) => (
            <PublicRow key={item.id} slug={slug} item={item} now={now} showUptime={showUptime} />
          ))}
        </ul>
      )}

      {page.incidents && <Incidents page={page} now={now} />}
    </>
  )
}

function PublicRow({ slug, item, now, showUptime }: { slug: string; item: PublicItem; now: number; showUptime: boolean }) {
  const stats = usePublicStats(slug, item.id, showUptime)
  const uptime = formatUptime(item.uptime_pct)
  return (
    <li className={`public-item row-${item.state}`}>
      <div className="public-item-head">
        <StateGlyph state={item.state} />
        <span className="public-name">{item.display_name}</span>
        <span className="public-state">
          {publicStateText(item, now)}
          {item.last_response_ms != null && item.state === 'up' && (
            <span className="muted">, {formatMs(item.last_response_ms)}</span>
          )}
        </span>
      </div>
      {showUptime && (
        <DayStrip
          name={item.display_name}
          days={stats.data ? dailyUptime(stats.data.buckets, 30, now) : null}
          uptime={uptime}
        />
      )}
    </li>
  )
}

function dayText(day: Day): string {
  const date = new Date(day.start).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  if (!day.total) return `${date}: no data`
  const failed = day.total - day.ok
  return `${date}: ${formatUptime((100 * day.ok) / day.total)} uptime${failed ? `, ${plural(failed, 'failed check')}` : ''}`
}

/** 30 days as bars, oldest left. Pointer or arrow keys read a day out below it. */
function DayStrip({ name, days, uptime }: { name: string; days: Day[] | null; uptime: string | null }) {
  const [active, setActive] = useState<number | null>(null)
  const count = days?.length ?? 30

  function onPointer(event: PointerEvent<HTMLDivElement>) {
    const rect = event.currentTarget.getBoundingClientRect()
    setActive(slotAt(event.clientX - rect.left, rect.width, count))
  }

  function onKey(event: KeyboardEvent<HTMLDivElement>) {
    const current = active ?? count - 1
    const next = event.key === 'ArrowLeft' ? current - 1 : event.key === 'ArrowRight' ? current + 1 : null
    if (next === null) return
    event.preventDefault()
    setActive(Math.min(count - 1, Math.max(0, next)))
  }

  return (
    <div className="day-strip">
      <div
        className={`day-bars${days ? '' : ' is-loading'}`}
        role="img"
        aria-label={`${name}, uptime by day over the last 30 days${uptime ? `: ${uptime}` : ''}. Use the arrow keys to read each day.`}
        tabIndex={days ? 0 : -1}
        onPointerMove={onPointer}
        onPointerDown={onPointer}
        onPointerLeave={() => setActive(null)}
        onKeyDown={onKey}
        onFocus={() => setActive((a) => a ?? count - 1)}
        onBlur={() => setActive(null)}
      >
        {(days ?? Array.from({ length: 30 }, () => null)).map((day, i) => (
          <span
            key={i}
            className={`day-bar strip-${day ? slotStatus(day) : 'none'}${i === active ? ' is-active' : ''}`}
          />
        ))}
      </div>
      <p className="day-caption" aria-live="polite">
        {active !== null && days ? (
          <span>{dayText(days[active])}</span>
        ) : (
          <>
            <span>30 days ago</span>
            {uptime && <span>{uptime} uptime</span>}
            <span>Today</span>
          </>
        )}
      </p>
    </div>
  )
}

function Incidents({ page, now }: { page: PublicPage; now: number }) {
  const incidents = page.incidents ?? []
  return (
    <section className="section" aria-labelledby="public-incidents">
      <h2 id="public-incidents" className="section-title">
        Recent incidents
      </h2>
      {incidents.length === 0 ? (
        <p className="muted">No incidents in the last 30 days.</p>
      ) : (
        <ul className="log">
          {incidents.map((i) => {
            const open = i.ended_at === null
            const duration = (open ? now : Date.parse(i.ended_at!)) - Date.parse(i.started_at)
            return (
              <li key={`${i.item_id}-${i.started_at}`} className={`log-row public-log${open ? ' is-open' : ''}`}>
                <span className="log-when">{formatWhen(i.started_at)}</span>
                <span className="log-what">
                  {i.display_name} {open ? 'is down' : 'was down'}
                </span>
                <span className="log-dur">{open ? `Ongoing, ${formatDuration(duration)}` : formatDuration(duration)}</span>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
