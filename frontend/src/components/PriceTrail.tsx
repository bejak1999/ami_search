import { money, shortDate } from '@/lib/format'
import { Tooltip } from '@/components/ui'
import clsx from 'clsx'

/**
 * One second-hand copy's asking price over time.
 *
 * AmiAmi marks a used copy down while it sits, a couple of hundred yen at a
 * time, and that is worth seeing at the moment you are looking at the price:
 * a copy that has come down twice in a fortnight is a different proposition
 * from one that has not moved since it was listed.
 *
 * A point is only recorded when the figure actually changed, so a copy that
 * has never been repriced has a trail of one and shows nothing at all —
 * a tooltip saying "no changes" would be noise on the overwhelming majority.
 */
export type TrailPoint = { at: string; price: number; in_stock?: boolean }

export function PriceTrail({
  trail,
  currency,
  children,
  className,
}: {
  trail?: TrailPoint[] | null
  currency: string
  children: React.ReactNode
  className?: string
}) {
  const points = (trail ?? []).filter((p) => typeof p.price === 'number')
  if (points.length < 2) return <>{children}</>

  const first = points[0].price
  const last = points[points.length - 1].price
  const total = last - first

  return (
    <Tooltip
      content={
        <div className="min-w-[13rem] space-y-1">
          <p className="text-[11px] font-medium">
            {points.length - 1} price change{points.length === 2 ? '' : 's'} on this copy
          </p>
          <table className="w-full text-[11px] tabular-nums">
            <tbody>
              {points.map((point, index) => {
                const step = index === 0 ? null : point.price - points[index - 1].price
                return (
                  <tr key={`${point.at}-${index}`}>
                    <td className="pr-3 text-faint">{shortDate(point.at)}</td>
                    <td className="pr-3 text-right">{money(point.price, currency)}</td>
                    <td
                      className={clsx(
                        'text-right',
                        step === null
                          ? 'text-faint'
                          : step < 0
                            ? 'text-positive'
                            : 'text-warning',
                      )}
                    >
                      {step === null
                        ? 'listed'
                        : `${step > 0 ? '+' : ''}${money(step, currency)}`}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          <p
            className={clsx(
              'border-t border-line pt-1 text-[11px]',
              total < 0 ? 'text-positive' : 'text-warning',
            )}
          >
            {total < 0 ? 'Down ' : 'Up '}
            {money(Math.abs(total), currency)} since it was listed
          </p>
        </div>
      }
    >
      <span className={clsx('cursor-help underline decoration-dotted underline-offset-2', className)}>
        {children}
      </span>
    </Tooltip>
  )
}
