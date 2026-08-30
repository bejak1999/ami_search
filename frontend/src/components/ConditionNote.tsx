import { Icon } from '@/components/Icon'
import clsx from 'clsx'

/**
 * What AmiAmi says about one used copy, beyond its condition grade.
 *
 * Two kinds arrive in the same red text on the product page. A fault explains
 * a price that would otherwise look startling — "[Discoloration] Upper body
 * skin area has become white. Both legs are sticky and have stains" — and a
 * bonus is something extra in the box, "Postcard is included".
 *
 * Both are worth carrying, because both only exist on the shop's own page and
 * nowhere else, and both are about this particular copy. They are shown apart
 * because they read completely differently: a bonus in a warning colour would
 * be good news dressed as bad.
 *
 * What does not appear is the shipping boilerplate the shop puts in the same
 * field. Eleven of nineteen sampled red passages were the one sentence every
 * oversized item carries, and repeating it everywhere buries the notes that
 * actually say something about a copy.
 */
export type ShopNote = { text: string; kind?: 'fault' | 'bonus' }

export function ConditionNote({
  notes,
  compact,
  className,
}: {
  notes?: ShopNote[] | null
  compact?: boolean
  className?: string
}) {
  if (!notes || notes.length === 0) return null

  const faults = notes.filter((n) => n.kind !== 'bonus')
  const bonuses = notes.filter((n) => n.kind === 'bonus')

  return (
    <div className={clsx('space-y-1.5', className)}>
      {faults.length > 0 && (
        <Note
          tone="fault"
          icon="alertTriangle"
          label="Condition note"
          lines={faults.map((n) => n.text)}
          compact={compact}
        />
      )}
      {bonuses.length > 0 && (
        <Note
          tone="bonus"
          icon="plus"
          label="Comes with"
          lines={bonuses.map((n) => n.text)}
          compact={compact}
        />
      )}
    </div>
  )
}

function Note({
  tone,
  icon,
  label,
  lines,
  compact,
}: {
  tone: 'fault' | 'bonus'
  icon: 'alertTriangle' | 'plus'
  label: string
  lines: string[]
  compact?: boolean
}) {
  return (
    <p
      className={clsx(
        'flex items-start gap-1.5 rounded-control border',
        tone === 'fault'
          ? 'border-warning/30 bg-warning/10 text-warning'
          : 'border-info/30 bg-info/10 text-info',
        compact ? 'px-2 py-1 text-[11px] leading-snug' : 'px-3 py-2 text-xs leading-relaxed',
      )}
    >
      <Icon
        name={icon}
        className={clsx('mt-0.5 shrink-0', compact ? 'h-3 w-3' : 'h-3.5 w-3.5')}
      />
      <span>
        {!compact && <span className="font-medium">{label}: </span>}
        {lines.join(' · ')}
      </span>
    </p>
  )
}
