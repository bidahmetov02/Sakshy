import { useEffect, useRef, type ReactNode } from 'react'

/** A modal question with one action. The native dialog handles focus and Escape. */
export function ConfirmDialog({
  open,
  title,
  children,
  confirm,
  danger,
  busy,
  onConfirm,
  onClose,
}: {
  open: boolean
  title: string
  children: ReactNode
  confirm: string
  danger?: boolean
  busy?: boolean
  onConfirm: () => void
  onClose: () => void
}) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    if (open && !dialog.open) dialog.showModal()
    if (!open && dialog.open) dialog.close()
  }, [open])

  return (
    <dialog ref={ref} className="dialog" onClose={onClose} aria-labelledby="dialog-title">
      <h2 id="dialog-title" className="dialog-title">
        {title}
      </h2>
      <div className="dialog-body">{children}</div>
      <div className="dialog-actions">
        <button type="button" className="button" onClick={onClose} autoFocus>
          Cancel
        </button>
        <button
          type="button"
          className={`button ${danger ? 'button-danger' : 'button-primary'}`}
          onClick={onConfirm}
          disabled={busy}
        >
          {confirm}
        </button>
      </div>
    </dialog>
  )
}
