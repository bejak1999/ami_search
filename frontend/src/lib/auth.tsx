import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { api, setUnauthorizedHandler } from '@/api/client'
import type { PublicConfig, User } from '@/api/types'
import { useTheme } from './theme'

interface AuthContextValue {
  user: User | null
  config: PublicConfig | null
  loading: boolean
  login: (identifier: string, password: string, remember: boolean) => Promise<void>
  register: (email: string, username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  refresh: () => Promise<void>
  patchUser: (changes: Partial<User>) => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [config, setConfig] = useState<PublicConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const { setTheme, setMode } = useTheme()

  // The server stores the user's preferred skin, so a new device picks it up.
  const applyAppearance = useCallback(
    (account: User) => {
      if (account.theme === 'midnight' || account.theme === 'sakura') setTheme(account.theme)
      if (['dark', 'light', 'system'].includes(account.color_mode)) {
        setMode(account.color_mode as 'dark' | 'light' | 'system')
      }
    },
    [setTheme, setMode],
  )

  const bootstrap = useCallback(async () => {
    try {
      const [publicConfig, me] = await Promise.allSettled([api.config(), api.auth.me()])
      if (publicConfig.status === 'fulfilled') setConfig(publicConfig.value)
      if (me.status === 'fulfilled') {
        setUser(me.value)
        applyAppearance(me.value)
      } else {
        setUser(null)
      }
    } finally {
      setLoading(false)
    }
  }, [applyAppearance])

  useEffect(() => {
    setUnauthorizedHandler(() => setUser(null))
    void bootstrap()
  }, [bootstrap])

  const login = useCallback(
    async (identifier: string, password: string, remember: boolean) => {
      const result = await api.auth.login({ identifier, password, remember })
      setUser(result.user)
      applyAppearance(result.user)
      setConfig(await api.config())
    },
    [applyAppearance],
  )

  const register = useCallback(
    async (email: string, username: string, password: string) => {
      const result = await api.auth.register({ email, username, password })
      setUser(result.user)
      setConfig(await api.config())
    },
    [],
  )

  const logout = useCallback(async () => {
    try {
      await api.auth.logout()
    } finally {
      setUser(null)
    }
  }, [])

  const refresh = useCallback(async () => {
    const me = await api.auth.me()
    setUser(me)
  }, [])

  const patchUser = useCallback((changes: Partial<User>) => {
    setUser((prev) => (prev ? { ...prev, ...changes } : prev))
  }, [])

  const value = useMemo(
    () => ({ user, config, loading, login, register, logout, refresh, patchUser }),
    [user, config, loading, login, register, logout, refresh, patchUser],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside AuthProvider')
  return context
}
