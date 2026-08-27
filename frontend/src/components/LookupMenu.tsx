import { useState } from 'react'
import type { Item } from '@/api/types'
import { Icon, type IconName } from './Icon'

/**
 * Looking a figure up elsewhere.
 *
 * Shop photos are often just the box, or a prototype shot, which tells you
 * very little about what actually arrives. These open the searches people run
 * by hand at that point, with the name already filled in.
 */
function searchTargets(item: Item) {
  // The shop's title carries a lot of retail noise. Strip the parts that only
  // confuse an image search.
  const clean = item.name
    .replace(/\((?:Pre-owned[^)]*|Released|Re-?run|Bonus)\)/gi, ' ')
    .replace(/\[[^\]]*\]/g, ' ')
    .replace(/\b(complete figure|figure|ver\.?|version)\b/gi, ' ')
    .replace(/\s+/g, ' ')
    .trim()

  const withContext = [item.series, item.character, clean].filter(Boolean).join(' ')
  const q = encodeURIComponent(clean || item.name)
  const rich = encodeURIComponent(withContext || item.name)

  const targets: { key: string; label: string; hint: string; icon: IconName; url: string }[] = [
    {
      key: 'google-images',
      label: 'Google Images',
      hint: 'What it actually looks like',
      icon: 'search',
      url: `https://www.google.com/search?tbm=isch&q=${rich}`,
    },
    {
      key: 'google',
      label: 'Google',
      hint: 'Reviews and shops',
      icon: 'link',
      url: `https://www.google.com/search?q=${rich}`,
    },
  ]

  if (item.mfc_url) {
    targets.unshift({
      key: 'mfc-entry',
      label: 'MyFigureCollection entry',
      hint: item.mfc_matched_by === 'jan' ? 'Matched by barcode' : 'Probable match',
      icon: 'tag',
      url: item.mfc_url,
    })
  } else {
    targets.push({
      key: 'mfc-search',
      label: 'Search MyFigureCollection',
      hint: item.jan_code ? 'By barcode' : 'By name',
      icon: 'tag',
      url: item.jan_code
        ? `https://myfigurecollection.net/?keywords=${item.jan_code}&_tb=item`
        : `https://myfigurecollection.net/item/browse/figure/?keywords=${q}`,
    })
  }

  if (item.jan_code) {
    targets.push({
      key: 'barcode',
      label: 'Barcode search',
      hint: item.jan_code,
      icon: 'search',
      url: `https://www.google.com/search?q=${encodeURIComponent(item.jan_code)}`,
    })
  }

  return targets
}

export function LookupMenu({ item }: { item: Item }) {
  const [open, setOpen] = useState(false)
  const targets = searchTargets(item)

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="btn-ghost px-2.5"
        title="Look this figure up elsewhere"
        aria-label="Look this figure up elsewhere"
        aria-expanded={open}
      >
        <Icon name="search" />
        <Icon name="chevronDown" className="-ml-1 h-3 w-3" />
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} aria-hidden="true" />
          <div className="absolute right-0 z-40 mt-1 w-72 overflow-hidden rounded-card border border-line bg-surface py-1 shadow-pop">
            <p className="px-3 py-1.5 text-[11px] leading-relaxed text-faint">
              Shop photos are often just the box. These open with the figure's name already
              filled in.
            </p>
            {targets.map((target) => (
              <a
                key={target.key}
                href={target.url}
                target="_blank"
                rel="noreferrer"
                onClick={() => setOpen(false)}
                className="flex items-center gap-2.5 px-3 py-2 text-sm transition-colors hover:bg-raised"
              >
                <Icon name={target.icon} className="h-4 w-4 text-accent" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate">{target.label}</span>
                  <span className="block truncate text-[11px] text-faint">{target.hint}</span>
                </span>
                <Icon name="external" className="h-3.5 w-3.5 text-faint" />
              </a>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
