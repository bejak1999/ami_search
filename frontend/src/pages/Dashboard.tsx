import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '@/api/client'
import { Icon } from '@/components/Icon'
import { ItemCard, ItemCardSkeleton } from '@/components/ItemCard'
import { Badge, Card, EmptyState, SectionTitle, Skeleton, Stat } from '@/components/ui'
import { money, relativeTime } from '@/lib/format'
import { useAuth } from '@/lib/auth'
import { TRIGGER_META } from '@/lib/triggers'

export function DashboardPage() {
  const navigate = useNavigate()
  const { user } = useAuth()
  const { data, isLoading } = useQuery({
    queryKey: ['dashboard'],
    queryFn: api.dashboard,
    refetchInterval: 60_000,
  })

  const hour = new Date().getHours()
  const greeting = hour < 5 ? 'Still up' : hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening'

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            {greeting}, {user?.username}
          </h1>
          <p className="mt-1 text-sm text-muted">
            {data?.next_check_at
              ? `Next watch check ${relativeTime(data.next_check_at)}.`
              : 'No watches are scheduled yet.'}
          </p>
        </div>
        <div className="flex gap-2">
          <Link to="/search" className="btn-ghost">
            <Icon name="search" />
            Search
          </Link>
          <Link to="/watches?new=1" className="btn-primary">
            <Icon name="plus" />
            New watch
          </Link>
        </div>
      </header>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {isLoading ? (
          Array.from({ length: 4 }).map((_, index) => <Skeleton key={index} className="h-[100px]" />)
        ) : (
          <>
            <Stat
              label="Active watches"
              value={data?.watches_active ?? 0}
              sub={`${data?.watches_total ?? 0} total`}
              icon="eye"
              tone="accent"
            />
            <Stat
              label="Alerts today"
              value={data?.alerts_24h ?? 0}
              sub={`${data?.alerts_7d ?? 0} in the last 7 days`}
              icon="bell"
              tone={data?.alerts_unread ? 'warning' : 'neutral'}
            />
            <Stat
              label="Items tracked"
              value={data?.items_tracked ?? 0}
              sub={`${data?.wishlist_count ?? 0} on the wishlist`}
              icon="box"
            />
            <Stat
              label="Collection value"
              value={
                data?.collection_value
                  ? money(data.collection_value, data.collection_currency)
                  : '—'
              }
              sub="Owned items at today's prices"
              icon="chart"
              tone="positive"
            />
          </>
        )}
      </div>

      <div className="grid gap-6 xl:grid-cols-[1.15fr_1fr]">
        <section>
          <SectionTitle
            title="Latest alerts"
            icon="bell"
            subtitle={
              data?.alerts_unread
                ? `${data.alerts_unread} unread`
                : 'Everything caught up'
            }
            action={
              <Link to="/alerts" className="btn-quiet text-sm">
                View all
                <Icon name="chevronRight" className="h-3.5 w-3.5" />
              </Link>
            }
          />
          <Card className="divide-y divide-line overflow-hidden">
            {isLoading ? (
              Array.from({ length: 4 }).map((_, index) => (
                <div key={index} className="flex gap-3 p-3">
                  <Skeleton className="h-14 w-14 shrink-0" />
                  <div className="flex-1 space-y-2 py-1">
                    <Skeleton className="h-3 w-3/4 rounded" />
                    <Skeleton className="h-3 w-1/3 rounded" />
                  </div>
                </div>
              ))
            ) : data?.recent_alerts.length ? (
              data.recent_alerts.map((alert) => {
                const meta = TRIGGER_META[alert.trigger]
                return (
                  <button
                    key={alert.id}
                    onClick={() =>
                      alert.item_id ? navigate(`/item/${alert.item_id}`) : navigate('/alerts')
                    }
                    className="flex w-full items-start gap-3 p-3 text-left transition-colors hover:bg-raised"
                  >
                    {alert.image_url ? (
                      <img
                        src={alert.image_url}
                        alt=""
                        loading="lazy"
                        className="h-14 w-14 shrink-0 rounded-lg object-cover"
                      />
                    ) : (
                      <span className="grid h-14 w-14 shrink-0 place-items-center rounded-lg bg-raised text-faint">
                        <Icon name={meta.icon} className="h-5 w-5" />
                      </span>
                    )}
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <Badge tone={meta.tone}>{meta.label}</Badge>
                        {!alert.read_at && <span className="h-1.5 w-1.5 rounded-full bg-accent" />}
                        <span className="ml-auto shrink-0 text-[11px] text-faint">
                          {relativeTime(alert.created_at)}
                        </span>
                      </div>
                      <p className="mt-1 line-clamp-2 text-[13px] font-medium leading-snug">
                        {alert.extra?.item_name || alert.title}
                      </p>
                      <p className="mt-0.5 text-xs text-muted tabular-nums">
                        {money(alert.price, alert.currency)}
                        {alert.landed_price
                          ? ` · ${money(alert.landed_price, alert.landed_currency)} landed`
                          : ''}
                      </p>
                    </div>
                  </button>
                )
              })
            ) : (
              <EmptyState
                icon="bell"
                title="No alerts yet"
                body="Create a watch with a target price. The first check records what already exists, then you only hear about genuine changes."
                action={
                  <Link to="/watches?new=1" className="btn-primary">
                    <Icon name="plus" />
                    Create a watch
                  </Link>
                }
              />
            )}
          </Card>
        </section>

        <section>
          <SectionTitle
            title="Wishlist, in stock now"
            icon="heart"
            subtitle="Cheapest first"
            action={
              <Link to="/collection" className="btn-quiet text-sm">
                Collection
                <Icon name="chevronRight" className="h-3.5 w-3.5" />
              </Link>
            }
          />
          {isLoading ? (
            <div className="grid-cards">
              {Array.from({ length: 3 }).map((_, index) => (
                <ItemCardSkeleton key={index} />
              ))}
            </div>
          ) : data?.cheapest_wishlist.length ? (
            <div className="grid-cards">
              {data.cheapest_wishlist.map((item) => (
                <ItemCard
                  key={item.id ?? item.code}
                  item={item}
                  compact
                  onOpen={(target) => target.id && navigate(`/item/${target.id}`)}
                />
              ))}
            </div>
          ) : (
            <EmptyState
              icon="heart"
              title="Nothing from your wishlist is available"
              body="Add figures to your wishlist and they show up here the moment they are back in stock."
              action={
                <Link to="/search" className="btn-ghost">
                  <Icon name="search" />
                  Find something
                </Link>
              }
            />
          )}

          {(data?.price_drops_7d ?? 0) > 0 && (
            <Card className="mt-4 flex items-center gap-3 p-4">
              <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-positive/15 text-positive">
                <Icon name="down" className="h-4.5 w-4.5" />
              </span>
              <div className="min-w-0">
                <p className="text-sm font-medium">
                  {data?.price_drops_7d} price {data?.price_drops_7d === 1 ? 'drop' : 'drops'} in the
                  last week
                </p>
                <p className="text-xs text-muted">Across everything you are watching.</p>
              </div>
              <Link to="/alerts?trigger=price_drop" className="btn-quiet ml-auto shrink-0 text-sm">
                Review
              </Link>
            </Card>
          )}
        </section>
      </div>
    </div>
  )
}
