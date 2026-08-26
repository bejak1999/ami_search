import clsx from 'clsx'

/**
 * One inline SVG sprite. Keeping the paths here avoids an icon dependency and
 * keeps the bundle small; every glyph is drawn on a 24x24 grid.
 */
export const ICONS = {
  dashboard: 'M4 13h6V4H4v9Zm0 7h6v-5H4v5Zm10 0h6V11h-6v9Zm0-16v5h6V4h-6Z',
  search: 'M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm10 2-4.35-4.35',
  compass: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20Zm4.24-14.24-2.12 6.36-6.36 2.12 2.12-6.36 6.36-2.12Z',
  eye: 'M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7Zm10 3a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z',
  bell: 'M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0',
  heart: 'M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8l1.1 1L12 21l7.7-7.6 1.1-1a5.5 5.5 0 0 0 0-7.8Z',
  settings:
    'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm7.4-3a7.4 7.4 0 0 0-.1-1.1l2-1.6-2-3.4-2.4 1a7.5 7.5 0 0 0-1.9-1.1l-.4-2.6h-4l-.4 2.6c-.7.3-1.3.6-1.9 1.1l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.2l-2 1.6 2 3.4 2.4-1c.6.5 1.2.8 1.9 1.1l.4 2.6h4l.4-2.6c.7-.3 1.3-.6 1.9-1.1l2.4 1 2-3.4-2-1.6c.1-.4.1-.7.1-1.1Z',
  shield: 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z',
  plus: 'M12 5v14M5 12h14',
  check: 'M20 6 9 17l-5-5',
  close: 'M18 6 6 18M6 6l12 12',
  chevronDown: 'm6 9 6 6 6-6',
  chevronRight: 'm9 18 6-6-6-6',
  chevronLeft: 'm15 18-6-6 6-6',
  external: 'M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14 21 3',
  refresh: 'M23 4v6h-6M1 20v-6h6M3.5 9a9 9 0 0 1 14.9-3.4L23 10M1 14l4.6 4.4A9 9 0 0 0 20.5 15',
  trash: 'M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6',
  edit: 'M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7M18.5 2.5a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5Z',
  play: 'm5 3 14 9-14 9V3Z',
  pause: 'M6 4h4v16H6zM14 4h4v16h-4z',
  tag: 'm20.6 13.4-7.2 7.2a2 2 0 0 1-2.8 0l-8.2-8.2A2 2 0 0 1 2 11V4a2 2 0 0 1 2-2h7a2 2 0 0 1 1.4.6l8.2 8.2a2 2 0 0 1 0 2.6ZM7 7h.01',
  chart: 'M3 3v18h18M7 15l4-4 3 3 5-6',
  box: 'm21 8-9-5-9 5v8l9 5 9-5V8ZM3 8l9 5 9-5M12 13v10',
  yen: 'm7 4 5 7 5-7M8 12h8M8 16h8M12 11v9',
  clock: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20ZM12 6v6l4 2',
  filter: 'M22 3H2l8 9.5V19l4 2v-8.5L22 3Z',
  logout: 'M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9',
  telegram: 'm22 2-7 20-4-9-9-4 20-7Z',
  mail: 'M4 4h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Zm18 2-10 7L2 6',
  discord: 'M9 12a1 1 0 1 0 0-2 1 1 0 0 0 0 2Zm6 0a1 1 0 1 0 0-2 1 1 0 0 0 0 2ZM7.5 5.5 6 5a16 16 0 0 0-2.5 13A14 14 0 0 0 8 20l1-1.6M16.5 5.5 18 5a16 16 0 0 1 2.5 13A14 14 0 0 1 16 20l-1-1.6M7 16.5c3.3 1.3 6.7 1.3 10 0',
  webhook: 'M18 16.98h-5.99c-1.1 0-1.95.94-2.48 1.9A4 4 0 1 1 8.15 14M10.5 8.5a4 4 0 1 1 6.9 3.9M6 9a4 4 0 0 1 6.6-3',
  push: 'M12 3a6 6 0 0 1 6 6v4l2 3H4l2-3V9a6 6 0 0 1 6-6ZM10 19a2 2 0 0 0 4 0',
  sparkle: 'm12 3 2.1 5.9L20 11l-5.9 2.1L12 19l-2.1-5.9L4 11l5.9-2.1L12 3Z',
  fire: 'M12 22c4 0 7-2.7 7-6.5 0-4-3-6-4-9-.6 2-2 3-3 3.5C10 8 9 5 9 3 6.5 5 5 8.5 5 11.5 5 16 7.9 22 12 22Z',
  down: 'M12 5v14m0 0-6-6m6 6 6-6',
  up: 'M12 19V5m0 0-6 6m6-6 6 6',
  info: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20Zm0-6v-4m0-4h.01',
  user: 'M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z',
  moon: 'M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z',
  sun: 'M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10ZM12 1v2m0 18v2M4.2 4.2l1.4 1.4m12.8 12.8 1.4 1.4M1 12h2m18 0h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4',
  monitor: 'M20 3H4a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2ZM8 21h8m-4-4v4',
  grid: 'M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z',
  list: 'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
  menu: 'M3 6h18M3 12h18M3 18h18',
  download: 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3',
  upload: 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12',
  link: 'M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7L12.2 19',
  alertTriangle: 'M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0ZM12 9v4m0 4h.01',
  inbox: 'M22 12h-6l-2 3h-4l-2-3H2M5.4 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.4-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.8 1.1Z',
} as const

export type IconName = keyof typeof ICONS

/** Does this class list already say how big the icon should be? */
const HAS_SIZE = /(?:^|\s)-?(?:h|w|size)-/

export function Icon({
  name,
  className,
  strokeWidth = 1.9,
}: {
  name: IconName
  className?: string
  strokeWidth?: number
}) {
  // An SVG with no width or height renders at its intrinsic size, which is
  // enormous next to body text. Falling back only on undefined was not enough:
  // clsx(false) yields an empty string, and a caller passing purely cosmetic
  // classes leaves the icon unsized too. So the default is applied whenever
  // nothing in the class list actually sets a size.
  const sized = className && HAS_SIZE.test(className) ? className : clsx('h-4 w-4', className)

  return (
    <svg
      viewBox="0 0 24 24"
      className={clsx('shrink-0', sized)}
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={ICONS[name]} />
    </svg>
  )
}
