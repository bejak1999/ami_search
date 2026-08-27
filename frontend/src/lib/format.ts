/** Formatting helpers shared across the UI. */

/**
 * The interface is written in English, so its dates and numbers are formatted
 * in English too. Following the browser's locale instead produced sentences
 * like "ran vor 2 Stunden" sitting inside an otherwise English page.
 *
 * Currency and date *values* are still locale-correct for en-GB conventions;
 * only the language is pinned.
 */
const LOCALE = 'en-GB'

const NO_DECIMAL_CURRENCIES = new Set(['JPY', 'KRW', 'VND'])

export function money(amount: number | null | undefined, currency = 'JPY'): string {
  if (amount === null || amount === undefined || Number.isNaN(amount)) return '—'
  const code = currency.toUpperCase()
  const fractionDigits = NO_DECIMAL_CURRENCIES.has(code) ? 0 : 2
  try {
    return new Intl.NumberFormat(LOCALE, {
      style: 'currency',
      currency: code,
      minimumFractionDigits: fractionDigits,
      maximumFractionDigits: fractionDigits,
    }).format(amount)
  } catch {
    return `${amount.toFixed(fractionDigits)} ${code}`
  }
}

export function compactMoney(amount: number | null | undefined, currency = 'JPY'): string {
  if (amount === null || amount === undefined) return '—'
  if (Math.abs(amount) < 10000) return money(amount, currency)
  const code = currency.toUpperCase()
  return `${new Intl.NumberFormat(LOCALE, { notation: 'compact', maximumFractionDigits: 1 }).format(amount)} ${code}`
}

export function percent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined) return '—'
  return `${value >= 0 ? '' : ''}${value.toFixed(digits)}%`
}

const RELATIVE = new Intl.RelativeTimeFormat(LOCALE, { numeric: 'auto' })
const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ['year', 31536000],
  ['month', 2592000],
  ['week', 604800],
  ['day', 86400],
  ['hour', 3600],
  ['minute', 60],
  ['second', 1],
]

export function relativeTime(value: string | Date | null | undefined): string {
  if (!value) return '—'
  const date = typeof value === 'string' ? new Date(value) : value
  if (Number.isNaN(date.getTime())) return '—'
  const seconds = (date.getTime() - Date.now()) / 1000
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size || unit === 'second') {
      return RELATIVE.format(Math.round(seconds / size), unit)
    }
  }
  return '—'
}

export function dateTime(value: string | Date | null | undefined): string {
  if (!value) return '—'
  const date = typeof value === 'string' ? new Date(value) : value
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat(LOCALE, { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

export function shortDate(value: string | Date | null | undefined): string {
  if (!value) return '—'
  const date = typeof value === 'string' ? new Date(value) : value
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat(LOCALE, { month: 'short', day: 'numeric' }).format(date)
}

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  const value = Math.max(0, Math.round(seconds))
  if (value < 60) return `${value}s`
  if (value < 3600) return `${Math.round(value / 60)} min`
  if (value < 86400) {
    const hours = value / 3600
    return `${hours % 1 === 0 ? hours : hours.toFixed(1)} h`
  }
  return `${Math.round(value / 86400)} d`
}

export function grams(value: number | null | undefined): string {
  if (!value) return '—'
  return value >= 1000 ? `${(value / 1000).toFixed(2)} kg` : `${value} g`
}

/** "1/7 Complete Figure (Hobby Stock)" reads better without the boilerplate. */
export function tidyName(name: string): string {
  return name.replace(/\s*\((?:Released|Pre-order)\)\s*$/i, '').trim()
}

export function initials(text: string): string {
  return text
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((word) => word[0]?.toUpperCase() ?? '')
    .join('')
}
