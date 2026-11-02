import { STATE_LABEL, type DisplayState } from '../lib/checks'

/**
 * Each state has its own shape as well as its own colour, so it reads without
 * colour: up is a disc, failing a half disc, down a diamond, pending a ring,
 * paused two bars.
 */
export function StateGlyph({ state, label = false }: { state: DisplayState; label?: boolean }) {
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
      </svg>
      {label ? <span>{STATE_LABEL[state]}</span> : <span className="visually-hidden">{STATE_LABEL[state]}</span>}
    </span>
  )
}
