import { useState } from 'react'
import { useAuth } from '@/lib/auth'
import { useTheme, THEMES } from '@/lib/theme'
import { Icon } from '@/components/Icon'
import { Field, Spinner } from '@/components/ui'
import clsx from 'clsx'

const HIGHLIGHTS = [
  {
    icon: 'bell' as const,
    title: 'Alerts that arrive in time',
    body: 'Telegram, browser push, ntfy, Discord or e-mail, fired within seconds of a listing appearing.',
  },
  {
    icon: 'chart' as const,
    title: 'Real price history',
    body: 'Every price and stock change is recorded, so you know whether today is actually a good day to buy.',
  },
  {
    icon: 'box' as const,
    title: 'The price you really pay',
    body: 'Shipping, customs duty and import VAT are estimated for you. Set targets on the landed total, not the sticker.',
  },
  {
    icon: 'compass' as const,
    title: 'Discover by tag',
    body: 'Items are cross-referenced with MyFigureCollection, so you can browse by character, series or tag.',
  },
]

export function LoginPage() {
  const { config, login, register } = useAuth()
  const { theme, setTheme } = useTheme()
  const firstRun = config ? !config.has_users : false
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [identifier, setIdentifier] = useState('')
  const [email, setEmail] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [remember, setRemember] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const registering = mode === 'register' || firstRun

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setError(null)
    setBusy(true)
    try {
      if (registering) await register(email.trim(), username.trim(), password)
      else await login(identifier.trim(), password, remember)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Something went wrong')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid min-h-dvh lg:grid-cols-2">
      {/* Marketing half. Hidden on small screens where the form is all that matters. */}
      <div className="relative hidden flex-col justify-between overflow-hidden border-r border-line bg-surface p-10 lg:flex">
        <div className="pointer-events-none absolute inset-0 hero-wash" aria-hidden="true" />
        <div className="relative">
          <div className="flex items-center gap-2.5">
            <span className="grid h-9 w-9 place-items-center rounded-xl bg-accent-gradient text-accent-ink shadow-sm">
              <Icon name="search" className="h-4.5 w-4.5" strokeWidth={2.4} />
            </span>
            <span className="text-lg font-semibold tracking-tight">AmiSearch</span>
          </div>
          <h1 className="mt-12 max-w-md text-4xl font-semibold leading-[1.1] tracking-tight text-balance">
            Stop finding out the figure sold out yesterday.
          </h1>
          <p className="mt-4 max-w-md text-[15px] leading-relaxed text-muted">
            Self-hosted price tracking and restock alerts for AmiAmi. Save a search with a target
            price, and get pushed the second something matches.
          </p>

          <ul className="mt-10 space-y-5">
            {HIGHLIGHTS.map((entry) => (
              <li key={entry.title} className="flex gap-3.5">
                <span className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-accent/12 text-accent">
                  <Icon name={entry.icon} className="h-4 w-4" />
                </span>
                <div className="max-w-sm">
                  <p className="text-sm font-medium">{entry.title}</p>
                  <p className="mt-0.5 text-[13px] leading-relaxed text-muted">{entry.body}</p>
                </div>
              </li>
            ))}
          </ul>
        </div>

        <div className="relative flex items-center gap-2 text-xs text-faint">
          <span>Skin</span>
          {THEMES.map((entry) => (
            <button
              key={entry.id}
              onClick={() => setTheme(entry.id)}
              className={clsx(
                'flex items-center gap-1.5 rounded-full border px-2.5 py-1 transition-colors',
                theme === entry.id
                  ? 'border-accent/50 text-accent'
                  : 'border-line hover:border-faint',
              )}
            >
              <span className="flex -space-x-1">
                {entry.swatch.map((colour) => (
                  <span
                    key={colour}
                    className="h-2.5 w-2.5 rounded-full ring-1 ring-canvas"
                    style={{ background: colour }}
                  />
                ))}
              </span>
              {entry.label}
            </button>
          ))}
        </div>
      </div>

      <div className="flex items-center justify-center px-5 py-12">
        <div className="w-full max-w-sm animate-fade-up">
          <div className="mb-8 lg:hidden">
            <div className="flex items-center gap-2.5">
              <span className="grid h-9 w-9 place-items-center rounded-xl bg-accent-gradient text-accent-ink">
                <Icon name="search" className="h-4.5 w-4.5" strokeWidth={2.4} />
              </span>
              <span className="text-lg font-semibold tracking-tight">AmiSearch</span>
            </div>
          </div>

          <h2 className="text-2xl font-semibold tracking-tight">
            {firstRun ? 'Create the first account' : registering ? 'Create an account' : 'Welcome back'}
          </h2>
          <p className="mt-1.5 text-sm text-muted">
            {firstRun
              ? 'This instance is empty. The first account becomes the administrator.'
              : registering
                ? 'Your watches, channels and collection stay private to your account.'
                : 'Sign in to reach your watches and alerts.'}
          </p>

          <form onSubmit={submit} className="mt-7 space-y-4">
            {registering ? (
              <>
                <Field label="E-mail">
                  <input
                    type="email"
                    required
                    autoComplete="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    className="field"
                    placeholder="you@example.com"
                  />
                </Field>
                <Field label="Username">
                  <input
                    required
                    minLength={3}
                    maxLength={64}
                    pattern="[A-Za-z0-9_.\-]+"
                    autoComplete="username"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    className="field"
                    placeholder="figurehunter"
                  />
                </Field>
              </>
            ) : (
              <Field label="Username or e-mail">
                <input
                  required
                  autoComplete="username"
                  value={identifier}
                  onChange={(e) => setIdentifier(e.target.value)}
                  className="field"
                  placeholder="figurehunter"
                />
              </Field>
            )}

            <Field
              label="Password"
              hint={registering ? 'At least 10 characters, mixing two character types.' : undefined}
            >
              <input
                type="password"
                required
                minLength={registering ? 10 : undefined}
                autoComplete={registering ? 'new-password' : 'current-password'}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="field"
                placeholder="••••••••••"
              />
            </Field>

            {!registering && (
              <label className="flex cursor-pointer items-center gap-2 text-sm text-muted">
                <input
                  type="checkbox"
                  checked={remember}
                  onChange={(e) => setRemember(e.target.checked)}
                  className="h-4 w-4 rounded border-line bg-raised accent-[rgb(var(--c-accent))]"
                />
                Stay signed in on this device
              </label>
            )}

            {error && (
              <div className="flex items-start gap-2 rounded-control border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
                <Icon name="alertTriangle" className="mt-0.5 h-4 w-4" />
                <span>{error}</span>
              </div>
            )}

            <button type="submit" disabled={busy} className="btn-primary w-full py-2.5">
              {busy && <Spinner className="h-4 w-4" />}
              {firstRun ? 'Create administrator' : registering ? 'Create account' : 'Sign in'}
            </button>
          </form>

          {!firstRun && config?.registration_open && (
            <p className="mt-6 text-center text-sm text-muted">
              {registering ? 'Already have an account?' : 'No account yet?'}{' '}
              <button
                onClick={() => {
                  setMode(registering ? 'login' : 'register')
                  setError(null)
                }}
                className="font-medium text-accent hover:underline"
              >
                {registering ? 'Sign in' : 'Create one'}
              </button>
            </p>
          )}
          {!firstRun && !config?.registration_open && mode === 'login' && (
            <p className="mt-6 text-center text-xs text-faint">
              Registration is closed on this instance.
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
