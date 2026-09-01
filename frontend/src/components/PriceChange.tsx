import clsx from 'clsx'
import type { PriceChange } from '@/api/types'
import { money, relativeTime } from '@/lib/format'
import { Tooltip } from './ui'

/**
 * What moved since the last check, said in a few words.
 *
 * The number alone is ambiguous for used stock: the price shown is the
 * cheapest of several graded copies, so it falls both when a copy is marked
 * down and when a rougher copy turns up beside it. Those are different
 * pieces of news and the label has to say which one happened.
 */
const LABEL: Record<PriceChange['kind'], string> = {
  markdown: 'same copy reduced',
  undercut: 'cheaper copy arrived',
  sold_out_cheapest: 'cheapest copy sold',
  increase: 'price raised',
  cheaper: 'cheaper',
  dearer: 'dearer',
  unavailable: 'no longer buyable',
}

const EXPLAIN: Record<PriceChange['kind'], string> = {
  markdown:
    'The very copy that set the price before has been marked down. Same copy, same grade, less money.',
  undercut:
    'A different copy has appeared and undercuts the one we saw last time. Check its grade before reading it as a bargain.',
  sold_out_cheapest:
    'Nothing got dearer. The cheapest copy is gone — most likely sold — and the next one up is what you see now.',
  increase: 'The same copy is being asked more for than it was at the last check.',
  cheaper: 'Cheaper than at the last check.',
  dearer: 'Dearer than at the last check.',
  unavailable: 'There is nothing buyable under this code any more.',
}

/** The colour a price is painted when it has moved. */
export function priceChangeClass(change: PriceChange | null | undefined): string | undefined {
  if (!change) return undefined
  return change.direction === 'up' ? 'text-danger' : 'text-positive'
}

/**
 * The delta beside the price, in the shop's own currency.
 *
 * Yen rather than the converted figure on purpose: the movement happened in
 * yen, and running it through today's exchange rate would mix a price change
 * with a currency change.
 */
export function PriceChangeTag({
  change,
  className,
}: {
  change: PriceChange | null | undefined
  className?: string
}) {
  if (!change) return null
  const up = change.direction === 'up'
  const amount =
    change.difference === null
      ? null
      : `${change.difference > 0 ? '+' : ''}${money(change.difference, change.currency)}`

  return (
    <Tooltip
      content={
        <div className="w-56 space-y-1 text-left text-xs">
          <p>{EXPLAIN[change.kind]}</p>
          {change.was !== null && change.now !== null && (
            <p className="tabular-nums text-muted">
              {money(change.was, change.currency)} → {money(change.now, change.currency)}
              {change.percent !== null && ` (${change.percent > 0 ? '+' : ''}${change.percent}%)`}
            </p>
          )}
          {change.kind === 'undercut' && change.copy_grade && (
            <p className="text-muted">The new cheapest copy is graded {change.copy_grade}.</p>
          )}
          {change.since && (
            <p className="text-faint">Compared with {relativeTime(change.since)}.</p>
          )}
        </div>
      }
    >
      <span
        className={clsx(
          'inline-flex cursor-help items-center gap-1 rounded-control px-1.5 py-0.5 text-xs font-medium tabular-nums',
          up ? 'bg-danger/15 text-danger' : 'bg-positive/15 text-positive',
          className,
        )}
      >
        {amount}
        <span className="font-normal opacity-80">{LABEL[change.kind]}</span>
        {change.kind === 'undercut' && change.copy_grade && (
          <span className="font-normal opacity-80">({change.copy_grade})</span>
        )}
      </span>
    </Tooltip>
  )
}
