import clsx from 'clsx'
import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import { useAuth } from '@/lib/auth'
import { useAlertStream } from '@/lib/events'
import { useTheme } from '@/lib/theme'
import { useToast } from '@/lib/toast'
import { money } from '@/lib/format'
import { Icon, type IconName } from './Icon'

const NAV: { to: string; label: string; icon: IconName; adminOnly?: boolean }[] = [
  { to: '/', label: 'Dashboard', icon: 'dashboard' },
  { to: '/search', label: 'Search', icon: 'search' },
  { to: '/discover', label: 'Discover', icon: 'compass' },
  { to: '/watches', label: 'Watches', icon: 'eye' },
  { to: '/alerts', label: 'Alerts', icon: 'bell' },
  { to: '/collection', label: 'Collection', icon: 'heart' },
  { to: '/settings', label: 'Settings', icon: 'settings' },
  { to: '/admin', label: 'Admin', icon: 'shield', adminOnly: true },
]

function Logo() {
  return (
    <div className="flex items-center gap-2.5">
      <span className="grid h-8 w-8 place-items-center rounded-xl bg-accent-gradient text-accent-ink shadow-sm">
        <Icon name="search" className="h-4 w-4" strokeWidth={2.4} />
      </span>
      <span className="text-[15px] font-semibold tracking-tight">AmiSearch</span>
    </div>
  )
}

function ThemeSwitch() {
  const { mode, setMode, theme, setTheme } = useTheme()
  return (
    <div className="flex items-center gap-1">
      <button
        onClick={() => setTheme(theme === 'midnight' ? 'sakura' : 'midnight')}
        className="btn-quiet px-2 py-1.5"
        title={`Switch to the ${theme === 'midnight' ? 'Sakura' : 'Midnight'} skin`}
      >
        <Icon name="sparkle" className="h-4 w-4" />
      </button>
      <button
        onClick={() => setMode(mode === 'dark' ? 'light' : 'dark')}
        className="btn-quiet px-2 py-1.5"
        title={mode === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
      >
        <Icon name={mode === 'dark' ? 'sun' : 'moon'} className="h-4 w-4" />
      </button>
    </div>
  )
}

function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const { user, logout } = useAuth()
  const { data: unread } = useQuery({
    queryKey: ['alerts', 'unread'],
    queryFn: () => api.alerts.unreadCount(),
    refetchInterval: 60_000,
  })
  const unreadCount = unread?.detail?.count ?? 0

  return (
    <div className="flex h-full flex-col gap-1 p-3">
      <div className="px-2 py-3">
        <Logo />
      </div>

      <nav className="flex flex-1 flex-col gap-0.5">
        {NAV.filter((entry) => !entry.adminOnly || user?.role === 'admin').map((entry) => (
          <NavLink
            key={entry.to}
            to={entry.to}
            end={entry.to === '/'}
            onClick={onNavigate}
            className={({ isActive }) =>
              clsx(
                'group flex items-center gap-2.5 rounded-control px-2.5 py-2 text-sm font-medium transition-colors',
                isActive
                  ? 'bg-accent/12 text-accent'
                  : 'text-muted hover:bg-raised hover:text-ink',
              )
            }
          >
            <Icon name={entry.icon} className="h-4 w-4" />
            <span className="flex-1">{entry.label}</span>
            {entry.to === '/alerts' && unreadCount > 0 && (
              <span className="rounded-full bg-accent px-1.5 py-0.5 text-[10px] font-bold leading-none text-accent-ink">
                {unreadCount > 99 ? '99+' : unreadCount}
              </span>
            )}
          </NavLink>
        ))}
      </nav>

      <div className="mt-auto space-y-2 border-t border-line pt-3">
        <div className="flex items-center gap-2.5 rounded-control px-2 py-1.5">
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-raised text-xs font-semibold uppercase text-muted">
            {user?.username?.slice(0, 2)}
          </span>
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-medium leading-tight">{user?.username}</p>
            <p className="truncate text-[11px] text-faint">{user?.email}</p>
          </div>
          <button onClick={() => void logout()} className="btn-quiet p-1.5" title="Sign out">
            <Icon name="logout" className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  )
}

export function AppShell() {
  const [mobileOpen, setMobileOpen] = useState(false)
  const location = useLocation()
  const toast = useToast()
  const queryClient = useQueryClient()
  const { user } = useAuth()

  useEffect(() => setMobileOpen(false), [location.pathname])

  // Live alerts: the toast appears the moment the poller fires, which is the
  // whole reason this app exists.
  useAlertStream(Boolean(user), (alert) => {
    queryClient.invalidateQueries({ queryKey: ['alerts'] })
    queryClient.invalidateQueries({ queryKey: ['dashboard'] })
    if (alert.suppressed) return
    toast.push({
      kind: 'success',
      title: alert.title,
      body: alert.price
        ? `${money(alert.price, alert.currency)}${
            alert.landed_price
              ? ` · ${money(alert.landed_price, alert.landed_currency)} landed`
              : ''
          }`
        : undefined,
      href: alert.url ?? undefined,
      image: alert.image_url,
    })
  })

  return (
    <div className="min-h-dvh bg-canvas">
      <div className="pointer-events-none fixed inset-x-0 top-0 h-72 hero-wash" aria-hidden="true" />

      {/* Desktop sidebar */}
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-60 border-r border-line bg-surface/70 backdrop-blur-xl lg:block">
        <Sidebar />
      </aside>

      {/* Mobile drawer */}
      {mobileOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div className="absolute inset-0 bg-black/50" onClick={() => setMobileOpen(false)} />
          <aside className="absolute inset-y-0 left-0 w-64 animate-slide-in border-r border-line bg-surface">
            <Sidebar onNavigate={() => setMobileOpen(false)} />
          </aside>
        </div>
      )}

      <div className="lg:pl-60">
        <header className="sticky top-0 z-20 flex h-14 items-center gap-3 border-b border-line bg-canvas/80 px-4 backdrop-blur-xl sm:px-6">
          <button
            onClick={() => setMobileOpen(true)}
            className="btn-quiet -ml-1.5 p-2 lg:hidden"
            aria-label="Open navigation"
          >
            <Icon name="menu" className="h-5 w-5" />
          </button>
          <div className="lg:hidden">
            <Logo />
          </div>
          <div className="ml-auto flex items-center gap-1">
            <ThemeSwitch />
          </div>
        </header>

        <main className="relative mx-auto w-full max-w-[1600px] px-4 py-6 sm:px-6 lg:py-8">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
