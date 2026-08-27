import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { TriggerType } from '@/api/types'
import { Icon } from '@/components/Icon'
import { Badge, Card, EmptyState, Skeleton } from '@/components/ui'
import { dateTime, money, relativeTime } from '@/lib/format'
import { TRIGGER_META, TRIGGER_OPTIONS } from '@/lib/triggers'
import { useToast } from '@/lib/toast'
import clsx from 'clsx'

export function AlertsPage() {
  const navigate = useNavigate()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [params, setParams] = useSearchParams()
  const [unreadOnly, setUnreadOnly] = useState(false)
  const trigger = (params.get('trigger') as TriggerType | null) ?? null

  const alerts = useQuery({
    queryKey: ['alerts', { trigger, unreadOnly }],
    queryFn: () =>
      api.alerts.list({ limit: 100, trigger: trigger ?? undefined, unread_only: unreadOnly }),
    refetchInterval: 45_000,
  })

  const markAll = useMutation({
    mutationFn: () => api.alerts.markAllRead(),
    onSuccess: (result) => {
      toast.success(result.message)
      void queryClient.invalidateQueries({ queryKey: ['alerts'] })
    },
  })

  const clear = useMutation({
    mutationFn: () => api.alerts.clear(true),
    onSuccess: (result) => {
      toast.success(result.message)
      void queryClient.invalidateQueries({ queryKey: ['alerts'] })
    },
  })

  const markRead = useMutation({
    mutationFn: (id: number) => api.alerts.markRead(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['alerts'] }),
  })

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Alerts</h1>
          <p className="mt-1 text-sm text-muted">
            Everything your watches have found, newest first.
          </p>
        </div>
        <div className="flex gap-2">
          <button onClick={() => markAll.mutate()} className="btn-ghost">
            <Icon name="check" />
            Mark all read
          </button>
          <button
            onClick={() => {
              if (confirm('Delete every alert you have already read?')) clear.mutate()
            }}
            className="btn-ghost text-danger"
          >
            <Icon name="trash" />
            Clear read
          </button>
        </div>
      </header>

      <div className="scroll-x flex gap-2 pb-1">
        <button
          onClick={() => setParams({})}
          className={clsx('chip shrink-0', !trigger && 'chip-active')}
        >
          All
        </button>
        {TRIGGER_OPTIONS.map((option) => (
          <button
            key={option.value}
            onClick={() => setParams({ trigger: option.value })}
            className={clsx('chip shrink-0', trigger === option.value && 'chip-active')}
          >
            <Icon name={TRIGGER_META[option.value].icon} className="h-3 w-3" />
            {option.label}
          </button>
        ))}
        <span className="mx-1 w-px shrink-0 bg-line" />
        <button
          onClick={() => setUnreadOnly((v) => !v)}
          className={clsx('chip shrink-0', unreadOnly && 'chip-active')}
        >
          Unread only
        </button>
      </div>

      {alerts.isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 6 }).map((_, index) => (
            <Skeleton key={index} className="h-20 rounded-card" />
          ))}
        </div>
      ) : alerts.data?.length ? (
        <div className="space-y-2">
          {alerts.data.map((alert) => {
            const meta = TRIGGER_META[alert.trigger]
            const drop =
              alert.previous_price && alert.price && alert.previous_price > alert.price
                ? (1 - alert.price / alert.previous_price) * 100
                : null
            return (
              <Card
                key={alert.id}
                hover
                className={clsx(
                  'flex cursor-pointer items-start gap-3.5 p-3.5',
                  !alert.read_at && 'border-l-[3px] border-l-accent',
                )}
                onClick={() => {
                  if (!alert.read_at) markRead.mutate(alert.id)
                  if (alert.item_id) navigate(`/item/${alert.item_id}`)
                }}
              >
                {alert.image_url ? (
                  <img
                    src={alert.image_url}
                    alt=""
                    loading="lazy"
                    className="h-16 w-16 shrink-0 rounded-lg object-cover"
                  />
                ) : (
                  <span className="grid h-16 w-16 shrink-0 place-items-center rounded-lg bg-raised text-faint">
                    <Icon name={meta.icon} className="h-5 w-5" />
                  </span>
                )}

                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone={meta.tone}>{meta.label}</Badge>
                    {/* An item can qualify several ways at once. One message,
                        named after the most important reason, with the rest
                        shown so nothing looks like it was missed. */}
                    {(alert.reasons ?? [])
                      .filter((reason) => reason !== alert.trigger)
                      .map((reason) => {
                        const extra = TRIGGER_META[reason as TriggerType]
                        return extra ? (
                          <Badge key={reason} tone="neutral">
                            {extra.label}
                          </Badge>
                        ) : null
                      })}
                    {alert.watch_label && (
                      <span className="truncate text-[11px] text-faint">{alert.watch_label}</span>
                    )}
                    {alert.extra?.summary && <Badge>Summary</Badge>}
                    <span className="ml-auto shrink-0 text-[11px] text-faint" title={dateTime(alert.created_at)}>
                      {relativeTime(alert.created_at)}
                    </span>
                  </div>

                  <p className="mt-1 line-clamp-2 text-sm font-medium leading-snug">
                    {alert.extra?.item_name || alert.title}
                  </p>

                  <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
                    <span className="font-semibold tabular-nums">
                      {money(alert.price, alert.currency)}
                    </span>
                    {drop !== null && (
                      <span className="text-positive tabular-nums">
                        −{Math.round(drop)}% from {money(alert.previous_price, alert.currency)}
                      </span>
                    )}
                    {alert.landed_price && (
                      <span className="text-muted tabular-nums">
                        {money(alert.landed_price, alert.landed_currency)} landed
                      </span>
                    )}
                    {Object.entries(alert.delivery_summary).map(([status, count]) => (
                      <span
                        key={status}
                        className={clsx(
                          'text-[11px]',
                          status === 'sent' && 'text-positive',
                          status === 'failed' && 'text-danger',
                          status === 'skipped' && 'text-faint',
                        )}
                      >
                        {count} {status}
                      </span>
                    ))}
                  </div>

                  {alert.body && alert.extra?.summary && (
                    <pre className="mt-2 max-h-24 overflow-hidden whitespace-pre-wrap border-t border-line pt-2 font-sans text-[11px] leading-relaxed text-muted">
                      {alert.body}
                    </pre>
                  )}
                </div>

                {alert.url && (
                  <a
                    href={alert.url}
                    target="_blank"
                    rel="noreferrer"
                    onClick={(event) => event.stopPropagation()}
                    className="btn-ghost shrink-0 px-2.5"
                    title="Open on shop"
                  >
                    <Icon name="external" />
                  </a>
                )}
              </Card>
            )
          })}
        </div>
      ) : (
        <EmptyState
          icon="bell"
          title={trigger ? 'Nothing of that kind yet' : 'No alerts yet'}
          body="Alerts appear here the moment a watch finds something. They also arrive on every notification channel you have switched on."
        />
      )}
    </div>
  )
}
