import { createContext, useCallback, useContext, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import clsx from 'clsx'

type ToastKind = 'success' | 'error' | 'info'

interface Toast {
  id: number
  kind: ToastKind
  title: string
  body?: string
  href?: string
  image?: string | null
}

interface ToastContextValue {
  push: (toast: Omit<Toast, 'id'>) => void
  success: (title: string, body?: string) => void
  error: (title: string, body?: string) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)
let nextId = 1

const ICONS: Record<ToastKind, string> = {
  success: 'M20 6 9 17l-5-5',
  error: 'M12 9v4m0 4h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z',
  info: 'M12 16v-4m0-4h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const push = useCallback(
    (toast: Omit<Toast, 'id'>) => {
      const id = nextId++
      setToasts((prev) => [...prev.slice(-4), { ...toast, id }])
      window.setTimeout(() => dismiss(id), toast.kind === 'error' ? 8000 : 5000)
    },
    [dismiss],
  )

  const value = useMemo(
    () => ({
      push,
      success: (title: string, body?: string) => push({ kind: 'success', title, body }),
      error: (title: string, body?: string) => push({ kind: 'error', title, body }),
    }),
    [push],
  )

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-[100] flex w-[min(92vw,26rem)] flex-col gap-2">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={clsx(
              'card pointer-events-auto flex animate-slide-in items-start gap-3 p-3 shadow-pop',
              toast.kind === 'success' && 'border-positive/40',
              toast.kind === 'error' && 'border-danger/40',
            )}
          >
            {toast.image ? (
              <img
                src={toast.image}
                alt=""
                className="h-11 w-11 shrink-0 rounded-lg object-cover"
                loading="lazy"
              />
            ) : (
              <span
                className={clsx(
                  'mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-full',
                  toast.kind === 'success' && 'bg-positive/15 text-positive',
                  toast.kind === 'error' && 'bg-danger/15 text-danger',
                  toast.kind === 'info' && 'bg-info/15 text-info',
                )}
              >
                <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <path d={ICONS[toast.kind]} />
                </svg>
              </span>
            )}
            <div className="min-w-0 flex-1">
              <p className="text-sm font-semibold leading-snug">{toast.title}</p>
              {toast.body && <p className="mt-0.5 text-xs leading-relaxed text-muted">{toast.body}</p>}
              {toast.href && (
                <a
                  href={toast.href}
                  target="_blank"
                  rel="noreferrer"
                  className="mt-1.5 inline-block text-xs font-medium text-accent hover:underline"
                >
                  Open on shop →
                </a>
              )}
            </div>
            <button
              onClick={() => dismiss(toast.id)}
              className="shrink-0 rounded p-1 text-faint transition-colors hover:text-ink"
              aria-label="Dismiss"
            >
              <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                <path d="M18 6 6 18M6 6l12 12" />
              </svg>
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastContextValue {
  const context = useContext(ToastContext)
  if (!context) throw new Error('useToast must be used inside ToastProvider')
  return context
}
