import { STATE_LABEL, type DisplayState } from '../lib/checks'

export type GlyphState = DisplayState | 'maintenance'

const LABEL: Record<GlyphState, string> = { ...STATE_LABEL, maintenance: 'Maintenance' }

/**
 * Each state has its own shape as well as its own colour, so it reads without
 * colour: up is a disc, failing a half disc, down a diamond, pending a ring,
 * paused two bars, maintenance a ring with a bar through it.
 */
export function StateGlyph({
  state,
  label = false,
  text,
}: {
  state: GlyphState
  /** Show the label beside the glyph; otherwise it is for screen readers only. */
  label?: boolean
  /** A label other than the state's name. */
  text?: string
}) {
  const name = text ?? LABEL[state]
  return (
    <span className={`glyph glyph-${state}`}>
      <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true">
        {state === 'up' && <circle cx="6" cy="6" r="5" />}
        {state === 'failing' && (
          <>
            <circle cx="6" cy="6" r="4.25" className="glyph-ring" />
            <path d="M6 1a5 5 0 0 1 0 10z" />
          </>
        )}
        {state === 'down' && <path d="M6 0.5 11.5 6 6 11.5 0.5 6z" />}
        {state === 'pending' && <circle cx="6" cy="6" r="4.25" className="glyph-ring" />}
        {state === 'paused' && (
          <>
            <rect x="2" y="1.5" width="2.75" height="9" rx="0.75" />
            <rect x="7.25" y="1.5" width="2.75" height="9" rx="0.75" />
          </>
        )}
        {state === 'maintenance' && (
          <>
            <circle cx="6" cy="6" r="4.25" className="glyph-ring" />
            <rect x="2.5" y="5.1" width="7" height="1.8" rx="0.9" />
          </>
        )}
      </svg>
      {label ? <span>{name}</span> : <span className="visually-hidden">{name}</span>}
    </span>
  )
}
