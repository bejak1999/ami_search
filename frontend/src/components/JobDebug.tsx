import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/api/client'
import { Icon } from '@/components/Icon'
import clsx from 'clsx'

/**
 * Look over one job's shoulder.
 *
 * Two halves, because they answer different questions. The state says what it
 * is working on right now — which slice, which ordering, which page — and
 * only the job itself can say that. The trail says what actually went to the
 * shop, with the query as it was sent, which is the half that settles an
 * argument about whether a setting took effect.
 *
 * Folded away by default and only polled while open: this is for when
 * something looks wrong, not a thing to watch all day.
 */
export function JobDebug({
  purpose,
  tag,
  className,
}: {
  purpose: string
  /** Which of a purpose's several jobs, when it has more than one. The
   *  catalogue is four slices sharing one purpose and one allowance. */
  tag?: string
  className?: string
}) {
  const [open, setOpen] = useState(false)

  const debug = useQuery({
    queryKey: ['jobDebug', purpose, tag],
    queryFn: () => api.admin.jobDebug(purpose, tag),
    enabled: open,
    refetchInterval: open ? 3_000 : false,
  })

  const d = debug.data?.detail as
    | {
        label: string
        doing: { what: string; for_seconds: number; [key: string]: unknown } | null
        recent: {
          at: number
          host: string
          ok: boolean
          url: string
          status: number | null
          ms: number | null
          ago_seconds: number
        }[]
        budget?: {
          total_per_minute: number
          running: string[]
          shares: { purpose: string; per_minute: number; running: boolean }[]
        }
      }
    | undefined

  return (
    <div className={className}>
      <button
        onClick={() => setOpen((was) => !was)}
        className="btn-ghost text-[11px]"
        title="What this job is doing, and the requests it just made"
      >
        <Icon name={open ? 'chevronDown' : 'chevronRight'} className="h-3 w-3" />
        Debug
      </button>

      {open && (
        <div className="mt-2 space-y-3 rounded-control border border-line bg-raised p-3">
          <div>
            <p className="text-[11px] font-medium text-muted">Right now</p>
            {d?.doing ? (
              <>
                <p className="mt-0.5 text-xs">{d.doing.what}</p>
                <dl className="mt-1.5 grid gap-x-4 gap-y-0.5 text-[11px] sm:grid-cols-2">
                  {Object.entries(d.doing)
                    .filter(
                      ([key, value]) =>
                        !['what', 'since', 'for_seconds'].includes(key) &&
                        value !== null &&
                        value !== undefined,
                    )
                    .map(([key, value]) => (
                      <div key={key} className="flex justify-between gap-3">
                        <dt className="text-faint">{key.replace(/_/g, ' ')}</dt>
                        <dd className="truncate font-mono text-[10px]">{String(value)}</dd>
                      </div>
                    ))}
                </dl>
                <p className="mt-1 text-[11px] text-faint">
                  for {d.doing.for_seconds}s
                </p>
              </>
            ) : (
              <p className="mt-0.5 text-xs text-faint">
                Idle — nothing running for this job at the moment.
              </p>
            )}
          </div>

          {d?.budget && (
            <div className="border-t border-line pt-2">
              <p className="text-[11px] font-medium text-muted">
                Sharing {d.budget.total_per_minute}/min to AmiAmi
              </p>
              <div className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-[11px]">
                {d.budget.shares.map((share) => (
                  <span
                    key={share.purpose}
                    className={share.running ? 'text-ink' : 'text-faint'}
                  >
                    {share.purpose} {share.per_minute}/min
                    {share.running ? ' · running' : ''}
                  </span>
                ))}
              </div>
            </div>
          )}

          <div className="border-t border-line pt-2">
            <p className="mb-1 text-[11px] font-medium text-muted">
              Last {d?.recent.length ?? 0} request{d?.recent.length === 1 ? '' : 's'}
            </p>
            {d?.recent.length ? (
              <div className="max-h-64 overflow-auto">
                <table className="w-full text-[10px] tabular-nums">
                  <tbody>
                    {d.recent.map((entry, index) => (
                      <tr key={`${entry.at}-${index}`} className="border-t border-line/50">
                        <td className="py-0.5 pr-2 text-faint">{ago(entry.ago_seconds)}</td>
                        <td
                          className={clsx(
                            'py-0.5 pr-2',
                            entry.ok ? 'text-positive' : 'text-danger',
                          )}
                        >
                          {entry.status ?? 'err'}
                        </td>
                        <td className="py-0.5 pr-2 text-right text-faint">
                          {entry.ms ? `${Math.round(entry.ms)}ms` : '—'}
                        </td>
                        <td className="py-0.5 font-mono text-muted">
                          <span className="block max-w-[28rem] truncate" title={entry.url}>
                            {entry.url || '—'}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="text-[11px] text-faint">
                Nothing recorded yet. Requests appear here as the job makes them; a restart
                clears the list.
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function ago(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  return `${Math.round(seconds / 3600)}h ago`
}
