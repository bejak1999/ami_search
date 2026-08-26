import clsx from 'clsx'
import type { ReactNode } from 'react'
import { Icon, type IconName } from './Icon'

export function Card({
  children,
  className,
  hover,
  onClick,
}: {
  children: ReactNode
  className?: string
  hover?: boolean
  onClick?: () => void
}) {
  return (
    <div className={clsx('card', hover && 'card-hover', className)} onClick={onClick}>
      {children}
    </div>
  )
}

export function SectionTitle({
  title,
  subtitle,
  action,
  icon,
}: {
  title: string
  subtitle?: string
  action?: ReactNode
  icon?: IconName
}) {
  return (
    <div className="mb-4 flex items-end justify-between gap-4">
      <div className="min-w-0">
        <h2 className="flex items-center gap-2 text-lg font-semibold tracking-tight">
          {icon && <Icon name={icon} className="h-4.5 w-4.5 text-accent" />}
          {title}
        </h2>
        {subtitle && <p className="mt-0.5 text-sm text-muted">{subtitle}</p>}
      </div>
      {action}
    </div>
  )
}

export function Badge({
  children,
  tone = 'neutral',
  className,
}: {
  children: ReactNode
  tone?: 'neutral' | 'accent' | 'positive' | 'warning' | 'danger' | 'info'
  className?: string
}) {
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold leading-tight',
        tone === 'neutral' && 'bg-raised text-muted ring-1 ring-inset ring-line',
        tone === 'accent' && 'bg-accent/15 text-accent ring-1 ring-inset ring-accent/25',
        tone === 'positive' && 'bg-positive/15 text-positive ring-1 ring-inset ring-positive/25',
        tone === 'warning' && 'bg-warning/15 text-warning ring-1 ring-inset ring-warning/25',
        tone === 'danger' && 'bg-danger/15 text-danger ring-1 ring-inset ring-danger/25',
        tone === 'info' && 'bg-info/15 text-info ring-1 ring-inset ring-info/25',
        className,
      )}
    >
      {children}
    </span>
  )
}

export function Spinner({ className }: { className?: string }) {
  return (
    <svg className={clsx('animate-spin', className ?? 'h-4 w-4')} viewBox="0 0 24 24" fill="none">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.2" strokeWidth="3" />
      <path
        d="M21 12a9 9 0 0 0-9-9"
        stroke="currentColor"
        strokeWidth="3"
        strokeLinecap="round"
      />
    </svg>
  )
}

export function EmptyState({
  icon = 'inbox',
  title,
  body,
  action,
}: {
  icon?: IconName
  title: string
  body?: string
  action?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center justify-center rounded-card border border-dashed border-line px-6 py-14 text-center">
      <span className="mb-3 grid h-12 w-12 place-items-center rounded-full bg-raised text-faint">
        <Icon name={icon} className="h-5 w-5" />
      </span>
      <p className="text-sm font-semibold">{title}</p>
      {body && <p className="mt-1 max-w-sm text-sm leading-relaxed text-muted">{body}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={clsx('skeleton rounded-lg', className)} />
}

export function Toggle({
  checked,
  onChange,
  label,
  hint,
  disabled,
}: {
  checked: boolean
  onChange: (value: boolean) => void
  label: string
  hint?: string
  disabled?: boolean
}) {
  return (
    <label
      className={clsx(
        'flex cursor-pointer items-start gap-3 select-none',
        disabled && 'cursor-not-allowed opacity-50',
      )}
    >
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={clsx(
          'relative mt-0.5 h-5 w-9 shrink-0 rounded-full transition-colors duration-200',
          checked ? 'bg-accent' : 'bg-line',
        )}
      >
        <span
          className={clsx(
            'absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform duration-200',
            checked ? 'translate-x-4' : 'translate-x-0.5',
          )}
        />
      </button>
      <span className="min-w-0">
        <span className="block text-sm font-medium leading-tight">{label}</span>
        {hint && <span className="mt-0.5 block text-xs leading-relaxed text-muted">{hint}</span>}
      </span>
    </label>
  )
}

export function SegmentedControl<T extends string>({
  value,
  onChange,
  options,
  className,
  size = 'md',
}: {
  value: T
  onChange: (value: T) => void
  options: { value: T; label: string; icon?: IconName }[]
  className?: string
  size?: 'sm' | 'md'
}) {
  return (
    <div
      className={clsx(
        'inline-flex rounded-control border border-line bg-raised p-0.5',
        className,
      )}
    >
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          onClick={() => onChange(option.value)}
          className={clsx(
            'inline-flex items-center gap-1.5 rounded-[calc(var(--r-control)-3px)] font-medium transition-all duration-150',
            size === 'sm' ? 'px-2.5 py-1 text-xs' : 'px-3 py-1.5 text-sm',
            value === option.value
              ? 'bg-surface text-ink shadow-sm'
              : 'text-muted hover:text-ink',
          )}
        >
          {option.icon && <Icon name={option.icon} className="h-3.5 w-3.5" />}
          {option.label}
        </button>
      ))}
    </div>
  )
}

export function Field({
  label,
  hint,
  error,
  children,
  className,
}: {
  label?: string
  hint?: string
  error?: string
  children: ReactNode
  className?: string
}) {
  return (
    <div className={className}>
      {label && <span className="label">{label}</span>}
      {children}
      {error ? (
        <p className="mt-1 text-xs text-danger">{error}</p>
      ) : hint ? (
        <p className="mt-1 text-xs leading-relaxed text-faint">{hint}</p>
      ) : null}
    </div>
  )
}

export function Stat({
  label,
  value,
  sub,
  icon,
  tone = 'neutral',
}: {
  label: string
  value: ReactNode
  sub?: ReactNode
  icon?: IconName
  tone?: 'neutral' | 'accent' | 'positive' | 'warning' | 'danger'
}) {
  return (
    <Card className="relative overflow-hidden p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs font-medium uppercase tracking-wide text-muted">{label}</p>
          <p className="mt-1.5 truncate text-2xl font-semibold tracking-tight">{value}</p>
          {sub && <p className="mt-1 truncate text-xs text-faint">{sub}</p>}
        </div>
        {icon && (
          <span
            className={clsx(
              'grid h-9 w-9 shrink-0 place-items-center rounded-xl',
              tone === 'neutral' && 'bg-raised text-muted',
              tone === 'accent' && 'bg-accent/15 text-accent',
              tone === 'positive' && 'bg-positive/15 text-positive',
              tone === 'warning' && 'bg-warning/15 text-warning',
              tone === 'danger' && 'bg-danger/15 text-danger',
            )}
          >
            <Icon name={icon} className="h-4.5 w-4.5" />
          </span>
        )}
      </div>
    </Card>
  )
}

export function Modal({
  open,
  onClose,
  title,
  subtitle,
  children,
  footer,
  size = 'md',
}: {
  open: boolean
  onClose: () => void
  title: string
  subtitle?: string
  children: ReactNode
  footer?: ReactNode
  size?: 'sm' | 'md' | 'lg' | 'xl'
}) {
  if (!open) return null
  const widths = {
    sm: 'max-w-md',
    md: 'max-w-xl',
    lg: 'max-w-3xl',
    xl: 'max-w-5xl',
  }
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4 sm:p-6">
      <div
        className="fixed inset-0 bg-black/55 backdrop-blur-sm"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        role="dialog"
        aria-modal="true"
        className={clsx(
          'relative my-auto w-full animate-fade-up rounded-card border border-line bg-surface shadow-pop',
          widths[size],
        )}
      >
        <div className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
          <div className="min-w-0">
            <h3 className="text-base font-semibold tracking-tight">{title}</h3>
            {subtitle && <p className="mt-0.5 text-sm text-muted">{subtitle}</p>}
          </div>
          <button onClick={onClose} className="btn-quiet -mr-1.5 -mt-1 p-1.5" aria-label="Close">
            <Icon name="close" />
          </button>
        </div>
        <div className="max-h-[70vh] overflow-y-auto px-5 py-4">{children}</div>
        {footer && (
          <div className="flex items-center justify-end gap-2 border-t border-line px-5 py-3.5">
            {footer}
          </div>
        )}
      </div>
    </div>
  )
}

export function Tooltip({ children, content }: { children: ReactNode; content: ReactNode }) {
  return (
    <span className="group/tip relative inline-flex">
      {children}
      <span className="pointer-events-none absolute bottom-full left-1/2 z-40 mb-2 w-max max-w-xs -translate-x-1/2 scale-95 rounded-lg border border-line bg-surface px-2.5 py-1.5 text-xs leading-relaxed text-ink opacity-0 shadow-pop transition-all duration-150 group-hover/tip:scale-100 group-hover/tip:opacity-100">
        {content}
      </span>
    </span>
  )
}

export function ProgressBar({ value, tone = 'accent' }: { value: number; tone?: string }) {
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-raised">
      <div
        className={clsx('h-full rounded-full transition-all duration-500', `bg-${tone}`)}
        style={{ width: `${Math.max(0, Math.min(100, value))}%` }}
      />
    </div>
  )
}
