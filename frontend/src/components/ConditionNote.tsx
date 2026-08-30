import { Icon } from '@/components/Icon'
import clsx from 'clsx'

/**
 * Why a used copy is marked down, in AmiAmi's own words.
 *
 * The shop prints this in red on the product page - "[Discoloration] Upper
 * body skin area has become white. Both legs are sticky and have stains" -
 * and it is usually the entire explanation for a price that otherwise looks
 * like a bargain. Leaving it out would make this application worse than the
 * page it summarises, so it travels with the price wherever the price goes.
 *
 * The shop puts shipping warnings in the same field, and those are filtered
 * out upstream: a parcel being awkward is not a defect, and showing it as one
 * would be worse than showing nothing.
 */
export function ConditionNote({
  note,
  compact,
  className,
}: {
  note?: string | null
  compact?: boolean
  className?: string
}) {
  if (!note) return null

  return (
    <p
      className={clsx(
        'flex items-start gap-1.5 rounded-control border border-warning/30 bg-warning/10 text-warning',
        compact ? 'px-2 py-1 text-[11px] leading-snug' : 'px-3 py-2 text-xs leading-relaxed',
        className,
      )}
    >
      <Icon
        name="alertTriangle"
        className={clsx('mt-0.5 shrink-0', compact ? 'h-3 w-3' : 'h-3.5 w-3.5')}
      />
      <span>
        {!compact && <span className="font-medium">Condition note: </span>}
        {note}
      </span>
    </p>
  )
}
