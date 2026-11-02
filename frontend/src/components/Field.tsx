import { cloneElement, isValidElement, type ReactElement, type ReactNode } from 'react'

/** A labelled form control with an optional hint and error, wired up for screen readers. */
export function Field({
  id,
  label,
  unit,
  optional,
  hint,
  error,
  children,
}: {
  id: string
  label: string
  unit?: string
  optional?: boolean
  hint?: ReactNode
  error?: string
  children: ReactNode
}) {
  const inputId = `f-${id}`
  const describedBy = [hint && `${inputId}-hint`, error && `${inputId}-error`].filter(Boolean).join(' ') || undefined
  return (
    <div className={`field${error ? ' has-error' : ''}`}>
      <label htmlFor={inputId}>
        {label}
        {unit && <span className="label-aside"> ({unit})</span>}
        {optional && <span className="label-aside"> (optional)</span>}
      </label>
      <FieldControl id={inputId} describedBy={describedBy} invalid={!!error}>
        {children}
      </FieldControl>
      {hint && (
        <p className="field-hint" id={`${inputId}-hint`}>
          {hint}
        </p>
      )}
      {error && (
        <p className="field-error" id={`${inputId}-error`}>
          {error}
        </p>
      )}
    </div>
  )
}

/** Wires the label, hint and error to the one control inside a Field. */
function FieldControl({
  id,
  describedBy,
  invalid,
  children,
}: {
  id: string
  describedBy?: string
  invalid: boolean
  children: ReactNode
}) {
  if (!isValidElement(children)) return children
  return cloneElement(children as ReactElement<Record<string, unknown>>, {
    id,
    'aria-describedby': describedBy,
    'aria-invalid': invalid || undefined,
  })
}
