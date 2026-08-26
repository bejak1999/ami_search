import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

export type ThemeName = 'midnight' | 'sakura'
export type ColorMode = 'dark' | 'light' | 'system'

export const THEMES: { id: ThemeName; label: string; blurb: string; swatch: string[] }[] = [
  {
    id: 'midnight',
    label: 'Midnight',
    blurb: 'Deep blue-grey with a single warm accent. Calm, dense, image-first.',
    swatch: ['#090b11', '#171b26', '#f97316'],
  },
  {
    id: 'sakura',
    label: 'Sakura',
    blurb: 'Pink and violet gradients, rounder cards, a bit more personality.',
    swatch: ['#110c18', '#f472b6', '#a78bfa'],
  },
]

const STORAGE_KEY = 'amisearch.appearance'

interface Appearance {
  theme: ThemeName
  mode: ColorMode
}

interface ThemeContextValue extends Appearance {
  resolvedMode: 'dark' | 'light'
  setTheme: (theme: ThemeName) => void
  setMode: (mode: ColorMode) => void
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

function readStored(): Appearance {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')
    return {
      theme: raw.theme === 'sakura' ? 'sakura' : 'midnight',
      mode: raw.mode === 'light' || raw.mode === 'system' ? raw.mode : 'dark',
    }
  } catch {
    return { theme: 'midnight', mode: 'dark' }
  }
}

function systemMode(): 'dark' | 'light' {
  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [appearance, setAppearance] = useState<Appearance>(readStored)
  const [systemPref, setSystemPref] = useState<'dark' | 'light'>(systemMode)

  useEffect(() => {
    const query = window.matchMedia('(prefers-color-scheme: light)')
    const listener = () => setSystemPref(query.matches ? 'light' : 'dark')
    query.addEventListener('change', listener)
    return () => query.removeEventListener('change', listener)
  }, [])

  const resolvedMode = appearance.mode === 'system' ? systemPref : appearance.mode

  useEffect(() => {
    const root = document.documentElement
    root.dataset.theme = appearance.theme
    root.dataset.mode = resolvedMode
    localStorage.setItem(STORAGE_KEY, JSON.stringify(appearance))

    // Keep the mobile browser chrome in step with the page background.
    const meta = document.querySelector('meta[name="theme-color"]')
    if (meta) {
      const canvas = getComputedStyle(root).getPropertyValue('--c-canvas').trim()
      if (canvas) meta.setAttribute('content', `rgb(${canvas.replace(/\s+/g, ',')})`)
    }
  }, [appearance, resolvedMode])

  const setTheme = useCallback(
    (theme: ThemeName) => setAppearance((prev) => ({ ...prev, theme })),
    [],
  )
  const setMode = useCallback((mode: ColorMode) => setAppearance((prev) => ({ ...prev, mode })), [])

  const value = useMemo(
    () => ({ ...appearance, resolvedMode, setTheme, setMode }),
    [appearance, resolvedMode, setTheme, setMode],
  )

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  const context = useContext(ThemeContext)
  if (!context) throw new Error('useTheme must be used inside ThemeProvider')
  return context
}
