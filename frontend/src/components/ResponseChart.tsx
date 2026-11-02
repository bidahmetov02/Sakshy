import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react'
import type { Range, Stats } from '../api/client'
import {
  areaPath,
  linePath,
  niceScale,
  segments,
  slotAt,
  slotStatus,
  statusRuns,
  timeTicks,
  toSlots,
  type Slot,
} from '../lib/chart'
import { formatMs } from '../lib/format'

const PLOT_H = 168
const STRIP_GAP = 12
const STRIP_H = 8
const AXIS_H = 24
const HEIGHT = PLOT_H + STRIP_GAP + STRIP_H + AXIS_H

const RANGE_LABEL: Record<Range, string> = { day: 'last 24 hours', week: 'last 7 days', month: 'last 30 days' }

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  const [width, setWidth] = useState(0)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const observer = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)))
    observer.observe(el)
    return () => observer.disconnect()
  }, [])
  return [ref, width] as const
}

function slotLabel(slot: Slot): string {
  const time = (t: number) => new Date(t).toLocaleTimeString(undefined, { timeStyle: 'short' })
  const day = new Date(slot.start).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  return `${day}, ${time(slot.start)}–${time(slot.end)}`
}

/** Average response time as a line, with the uptime of each slot as a strip beneath it. */
export function ResponseChart({ stats, range, stale }: { stats: Stats; range: Range; stale: boolean }) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const [active, setActive] = useState<number | null>(null)
  const slots = useMemo(() => toSlots(stats.buckets), [stats.buckets])
  const n = slots.length
  const hasData = slots.some((s) => s.total)

  const geometry = useMemo(() => {
    if (!width || !n) return null
    const peak = Math.max(0, ...slots.map((s) => s.avg ?? 0))
    const scale = niceScale(peak)
    const slotW = width / n
    const x = (i: number) => (i + 0.5) * slotW
    const y = (v: number) => PLOT_H - (v / scale.max) * (PLOT_H - 8)
    const lines = segments(slots, x, y)
    const span = [slots[0].start, slots[n - 1].end] as const
    const tx = (t: number) => ((t - span[0]) / (span[1] - span[0])) * width
    const ticks = timeTicks(span[0], span[1], Math.max(2, Math.floor(width / 72)))
    return { scale, slotW, x, y, lines, tx, ticks }
  }, [slots, width, n])

  function onPointer(event: PointerEvent<SVGSVGElement>) {
    const rect = event.currentTarget.getBoundingClientRect()
    setActive(slotAt(event.clientX - rect.left, rect.width, n))
  }

  function onKey(event: KeyboardEvent<SVGSVGElement>) {
    const current = active ?? n - 1
    const next =
      event.key === 'ArrowLeft' ? current - 1
      : event.key === 'ArrowRight' ? current + 1
      : event.key === 'Home' ? 0
      : event.key === 'End' ? n - 1
      : null
    if (next === null) return
    event.preventDefault()
    setActive(Math.min(n - 1, Math.max(0, next)))
  }

  const slot = active !== null ? slots[active] : null
  const status = slot ? slotStatus(slot) : null

  return (
    <figure className={`chart${stale ? ' is-stale' : ''}`}>
      <div className="chart-plot" ref={ref}>
        {geometry && (
          <svg
            width={width}
            height={HEIGHT}
            role="img"
            aria-label={`Average response time and uptime, ${RANGE_LABEL[range]}. Use the arrow keys to read each period.`}
            tabIndex={0}
            onPointerMove={onPointer}
            onPointerDown={onPointer}
            onPointerLeave={() => setActive(null)}
            onKeyDown={onKey}
            onFocus={() => setActive((a) => a ?? n - 1)}
            onBlur={() => setActive(null)}
          >
            {geometry.scale.ticks.map((t) => (
              <g key={t} className="chart-grid">
                <line x1={0} x2={width} y1={geometry.y(t)} y2={geometry.y(t)} />
                {t > 0 && (
                  <text x={0} y={geometry.y(t) - 5}>
                    {formatMs(t)}
                  </text>
                )}
              </g>
            ))}

            {geometry.lines.map((points, i) =>
              points.length > 1 ? (
                <g key={i}>
                  <path className="chart-area" d={areaPath(points, PLOT_H)} />
                  <path className="chart-line" d={linePath(points)} />
                </g>
              ) : (
                <circle key={i} className="chart-dot" cx={points[0][0]} cy={points[0][1]} r={2} />
              ),
            )}

            {!hasData && (
              <text className="chart-empty" x={width / 2} y={PLOT_H / 2} textAnchor="middle">
                No runs in this period yet.
              </text>
            )}

            {statusRuns(slots).map((run, i, runs) => {
              const x0 = run.from * geometry.slotW
              const w = (run.to - run.from) * geometry.slotW - (i < runs.length - 1 ? 2 : 0)
              if (w <= 0) return null
              return (
                <rect
                  key={run.from}
                  className={`strip strip-${run.status}`}
                  x={x0}
                  y={PLOT_H + STRIP_GAP}
                  width={w}
                  height={STRIP_H}
                  rx={Math.min(STRIP_H / 2, w / 2)}
                />
              )
            })}

            {geometry.ticks.ticks.map((t) => (
              <text
                key={t}
                className="chart-tick"
                x={Math.min(width - 2, Math.max(2, geometry.tx(t)))}
                y={HEIGHT - 6}
                textAnchor={geometry.tx(t) < 24 ? 'start' : geometry.tx(t) > width - 24 ? 'end' : 'middle'}
              >
                {geometry.ticks.daily
                  ? new Date(t).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
                  : new Date(t).toLocaleTimeString(undefined, { timeStyle: 'short' })}
              </text>
            ))}

            {slot && active !== null && (
              <g className="chart-cursor">
                <line x1={geometry.x(active)} x2={geometry.x(active)} y1={0} y2={PLOT_H + STRIP_GAP + STRIP_H} />
                {slot.avg != null && (
                  <circle cx={geometry.x(active)} cy={geometry.y(slot.avg)} r={4} />
                )}
              </g>
            )}
          </svg>
        )}

        {slot && geometry && active !== null && (
          <div
            className="chart-tip"
            style={{
              left: geometry.x(active),
              // Beside the cursor, on whichever side has room.
              transform: geometry.x(active) > width / 2 ? 'translateX(calc(-100% - 12px))' : 'translateX(12px)',
            }}
            aria-live="polite"
          >
            <p className="chart-tip-value">
              {!slot.total ? 'No checks ran' : slot.avg != null ? formatMs(slot.avg) : 'No response'}
            </p>
            {slot.max != null && <p className="chart-tip-row">average, slowest {formatMs(slot.max)}</p>}
            {!!slot.total && (
              <p className="chart-tip-row">
                <span className={`tip-key strip-${status}`} aria-hidden="true" />
                {slot.ok} of {slot.total} checks passed
              </p>
            )}
            <p className="chart-tip-time">{slotLabel(slot)}</p>
          </div>
        )}
      </div>

      <details className="chart-table">
        <summary>Show as table</summary>
        <DataTable slots={slots} />
      </details>
    </figure>
  )
}

function DataTable({ slots }: { slots: Slot[] }) {
  const rows = slots.filter((s) => s.total).reverse()
  if (!rows.length) return <p className="muted">No runs in this period yet.</p>
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th scope="col">Period</th>
            <th scope="col" className="num">
              Passed
            </th>
            <th scope="col" className="num">
              Average
            </th>
            <th scope="col" className="num">
              Slowest
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.start}>
              <td>{slotLabel(s)}</td>
              <td className="num">
                {s.ok} of {s.total}
              </td>
              <td className="num">{formatMs(s.avg)}</td>
              <td className="num">{formatMs(s.max)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
