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

/**
 * Turn one copy's joined note back into its statements.
 *
 * The per-copy note travels as text because that is how it is stored; the
 * split and the fault/bonus reading are the same ones the server applies.
 */
export function noteParts(note?: string | null): ShopNote[] {
  if (!note) return []
  return note.split(' · ').map((text) => ({
    text,
    kind: /\b(?:is|are)\s+included\b|^\s*(?:includes|comes with|bonus)\b/i.test(text) &&
      !/\bnot\b|\bmissing\b|\bwithout\b/i.test(text)
      ? 'bonus'
      : 'fault',
  }))
}

export function ConditionNote({
  notes,
  about,
  compact,
  className,
}: {
  notes?: ShopNote[] | null
  /** Which copy this is about, when it is not obvious from where it sits. */
  about?: string | null
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
          about={about}
          compact={compact}
        />
      )}
      {bonuses.length > 0 && (
        <Note
          tone="bonus"
          icon="plus"
          label="Comes with"
          lines={bonuses.map((n) => n.text)}
          about={about}
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
  about,
  compact,
}: {
  tone: 'fault' | 'bonus'
  icon: 'alertTriangle' | 'plus'
  label: string
  lines: string[]
  about?: string | null
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
        {!compact && (
          <span className="font-medium">
            {label}
            {about ? ` on the ${about}` : ''}:{' '}
          </span>
        )}
        {lines.join(' · ')}
      </span>
    </p>
  )
}
