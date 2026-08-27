import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { Grade, Item, PriceBasis, Watch } from '@/api/types'
import { useAuth } from '@/lib/auth'
import { useToast } from '@/lib/toast'
import { duration, money, tidyName } from '@/lib/format'
import { Icon } from './Icon'
import { Badge, Field, Modal, SegmentedControl, Spinner, Toggle, Tooltip } from './ui'
import clsx from 'clsx'

/**
 * A product code names a listing, not a product. AmiAmi sells the first-hand
 * copy under FIGURE-x and every pre-owned copy under FIGURE-x-R, and the
 * trailing digits on a link (FIGURE-x-R032) identify one graded copy of it.
 */
function isPreownedCode(code: string): boolean {
  return /-R(\d+)?$/i.test(code.trim())
}

function toListingCode(code: string, which: 'new' | 'preowned'): string {
  // Drop any single-copy suffix first: watching one graded copy would stop
  // working the moment that particular copy sells.
  const base = code.trim().replace(/-R\d+$/i, '-R').replace(/-R$/i, '')
  return which === 'preowned' ? `${base}-R` : base
}

// AmiAmi grades the figure and its box separately, best first.
const GRADES: Grade[] = ['S', 'A', 'B+', 'B', 'C', 'D']

const INTERVAL_PRESETS = [
  { seconds: 15, label: '15 s', note: 'Sniping. Use sparingly.' },
  { seconds: 60, label: '1 min', note: 'Aggressive' },
  { seconds: 300, label: '5 min', note: 'Recommended' },
  { seconds: 900, label: '15 min', note: 'Relaxed' },
  { seconds: 3600, label: '1 hour', note: 'Background' },
  { seconds: 86400, label: 'Daily', note: 'Digest pace' },
]

export interface WatchEditorProps {
  open: boolean
  onClose: () => void
  onSaved: (watch: Watch) => void
  watch?: Watch | null
  seedItem?: Partial<Item> | null
}

interface FormState {
  label: string
  kind: 'search' | 'item'
  query: string
  item_code: string
  condition: 'any' | 'new' | 'preowned'
  stock_filter: 'any' | 'in_stock' | 'preorder' | 'backorder'
  min_item_grade: Grade | ''
  min_box_grade: Grade | ''
  exclude: string
  target_price: string
  price_basis: PriceBasis
  target_currency: string
  notify_on_price_below: boolean
  notify_on_restock: boolean
  notify_on_new_match: boolean
  notify_on_price_drop_pct: string
  adaptive: boolean
  interval_seconds: number
  priority: number
  cooldown_seconds: number
  max_alerts_per_day: number
  quiet_enabled: boolean
  quiet_start: string
  quiet_end: string
  quiet_urgent: boolean
  channel_ids: number[]
  enabled: boolean
}

function initialState(
  watch: Watch | null | undefined,
  seed: Partial<Item> | null | undefined,
): FormState {
  if (watch) {
    return {
      label: watch.label,
      kind: watch.kind,
      query: watch.query,
      item_code: watch.item_code ?? '',
      condition: watch.condition,
      stock_filter: watch.stock_filter,
      min_item_grade: watch.min_item_grade ?? '',
      min_box_grade: watch.min_box_grade ?? '',
      exclude: (watch.filters?.exclude_keywords ?? []).join(', '),
      target_price: watch.target_price?.toString() ?? '',
      price_basis: watch.price_basis,
      target_currency: watch.target_currency,
      notify_on_price_below: watch.notify_on_price_below,
      notify_on_restock: watch.notify_on_restock,
      notify_on_new_match: watch.notify_on_new_match,
      notify_on_price_drop_pct: watch.notify_on_price_drop_pct?.toString() ?? '',
      adaptive: watch.adaptive,
      interval_seconds: watch.interval_seconds ?? 300,
      priority: watch.priority,
      cooldown_seconds: watch.cooldown_seconds,
      max_alerts_per_day: watch.max_alerts_per_day,
      quiet_enabled: watch.quiet_hours?.enabled ?? false,
      quiet_start: watch.quiet_hours?.start ?? '23:00',
      quiet_end: watch.quiet_hours?.end ?? '07:00',
      quiet_urgent: watch.quiet_hours?.urgent_override ?? true,
      channel_ids: watch.channel_ids ?? [],
      enabled: watch.enabled,
    }
  }

  const isItem = Boolean(seed?.code)
  return {
    label: seed?.name ? tidyName(seed.name).slice(0, 90) : '',
    kind: isItem ? 'item' : 'search',
    query: isItem ? '' : (seed?.name ?? ''),
    item_code: seed?.code ?? '',
    condition: seed?.condition === 'preowned' ? 'preowned' : 'any',
    stock_filter: 'any',
    min_item_grade: '',
    min_box_grade: '',
    exclude: '',
    // Seeding the target just under the current price is the setting people
    // reach for anyway, and it makes the watch immediately meaningful.
    target_price: seed?.price ? Math.floor(seed.price * 0.85).toString() : '',
    price_basis: 'listed',
    target_currency: seed?.currency ?? 'JPY',
    notify_on_price_below: true,
    notify_on_restock: true,
    notify_on_new_match: !isItem,
    notify_on_price_drop_pct: '',
    adaptive: true,
    interval_seconds: 300,
    priority: 0,
    cooldown_seconds: 1800,
    max_alerts_per_day: 50,
    quiet_enabled: false,
    quiet_start: '23:00',
    quiet_end: '07:00',
    quiet_urgent: true,
    channel_ids: [],
    enabled: true,
  }
}

export function WatchEditor({ open, onClose, onSaved, watch, seedItem }: WatchEditorProps) {
  const { user } = useAuth()
  const toast = useToast()
  const [tab, setTab] = useState<'what' | 'when' | 'how'>('what')
  const [form, setForm] = useState<FormState>(() => initialState(watch, seedItem))
  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }))

  const { data: channels } = useQuery({ queryKey: ['channels'], queryFn: api.channels.list })

  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      watch ? api.watches.update(watch.id, body) : api.watches.create(body),
    onSuccess: onSaved,
    onError: (error) => toast.error('Could not save the watch', (error as Error).message),
  })

  const landedCurrency = user?.display_currency ?? 'EUR'
  const currencyForTarget = form.price_basis === 'landed' ? landedCurrency : 'JPY'

  // Keep the target currency honest: a landed target is always in the
  // display currency, a listed target is always in the shop's currency.
  useMemo(() => {
    if (form.target_currency !== currencyForTarget) {
      setForm((prev) => ({ ...prev, target_currency: currencyForTarget }))
    }
  }, [currencyForTarget, form.target_currency])

  function submit() {
    const body: Record<string, unknown> = {
      label: form.label.trim(),
      kind: form.kind,
      query: form.query.trim(),
      item_code: form.kind === 'item' ? form.item_code.trim() : null,
      condition: form.condition,
      stock_filter: form.stock_filter,
      min_item_grade: form.min_item_grade || null,
      min_box_grade: form.min_box_grade || null,
      filters: {
        exclude_keywords: form.exclude
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean),
      },
      target_price: form.target_price ? Number(form.target_price) : null,
      price_basis: form.price_basis,
      target_currency: currencyForTarget,
      notify_on_price_below: form.notify_on_price_below,
      notify_on_restock: form.notify_on_restock,
      notify_on_new_match: form.notify_on_new_match,
      notify_on_price_drop_pct: form.notify_on_price_drop_pct
        ? Number(form.notify_on_price_drop_pct)
        : null,
      enabled: form.enabled,
      adaptive: form.adaptive,
      interval_seconds: form.interval_seconds,
      priority: form.priority,
      cooldown_seconds: form.cooldown_seconds,
      max_alerts_per_day: form.max_alerts_per_day,
      quiet_hours: {
        enabled: form.quiet_enabled,
        start: form.quiet_start,
        end: form.quiet_end,
        urgent_override: form.quiet_urgent,
      },
      channel_ids: form.channel_ids,
    }
    if (watch) delete body.kind
    save.mutate(body)
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      size="lg"
      title={watch ? 'Edit watch' : 'New watch'}
      subtitle={
        form.kind === 'item'
          ? 'Follow one specific listing.'
          : 'Follow a search. Anything new that matches becomes an alert.'
      }
      footer={
        <>
          <button onClick={onClose} className="btn-ghost">
            Cancel
          </button>
          <button onClick={submit} disabled={save.isPending} className="btn-primary">
            {save.isPending && <Spinner className="h-4 w-4" />}
            {watch ? 'Save changes' : 'Create watch'}
          </button>
        </>
      }
    >
      <div className="mb-5">
        <SegmentedControl
          value={tab}
          onChange={setTab}
          className="w-full"
          options={[
            { value: 'what', label: 'What to watch', icon: 'search' },
            { value: 'when', label: 'When to alert', icon: 'yen' },
            { value: 'how', label: 'How often', icon: 'clock' },
          ]}
        />
      </div>

      {tab === 'what' && (
        <div className="space-y-4">
          {!watch && (
            <Field label="Watch type">
              <SegmentedControl
                value={form.kind}
                onChange={(kind) => set('kind', kind)}
                options={[
                  { value: 'search', label: 'A search' },
                  { value: 'item', label: 'One item' },
                ]}
              />
            </Field>
          )}

          {form.kind === 'item' ? (
            <>
              <Field
                label="Product code or link"
                hint="Paste an amiami.com URL and the code is extracted for you."
              >
                <input
                  value={form.item_code}
                  onChange={(e) => set('item_code', e.target.value)}
                  className="field font-mono text-sm"
                  placeholder="FIGURE-153570-R"
                />
              </Field>

              {/* A product code is a listing, not a product: AmiAmi sells the
                  new copy under FIGURE-x and the pre-owned ones under
                  FIGURE-x-R. Choosing here rewrites the code, because a
                  condition filter on a single listing would be a setting that
                  silently does nothing. */}
              <Field
                label="Which listing"
                hint={
                  isPreownedCode(form.item_code)
                    ? 'Watching the pre-owned listing, which covers every graded copy on offer.'
                    : 'Watching the first-hand listing.'
                }
              >
                <SegmentedControl
                  value={isPreownedCode(form.item_code) ? 'preowned' : 'new'}
                  onChange={(which) =>
                    set('item_code', toListingCode(form.item_code, which as 'new' | 'preowned'))
                  }
                  options={[
                    { value: 'new', label: 'New' },
                    { value: 'preowned', label: 'Pre-owned' },
                  ]}
                />
              </Field>
            </>
          ) : (
            <Field
              label="Search terms"
              hint="Matched against the English product title. Character names work better than series names."
            >
              <input
                value={form.query}
                onChange={(e) => set('query', e.target.value)}
                className="field"
                placeholder="Symphogear Tsubasa"
              />
            </Field>
          )}

          <Field label="Name for this watch" hint="Shown in alerts and in your watch list.">
            <input
              value={form.label}
              onChange={(e) => set('label', e.target.value)}
              className="field"
              placeholder="Tsubasa 1/7 under 12k"
            />
          </Field>

          <div className="grid gap-4 sm:grid-cols-2">
            {form.kind === 'search' && (
              <Field label="Condition">
                <SegmentedControl
                  size="sm"
                  value={form.condition}
                  onChange={(condition) => set('condition', condition)}
                  options={[
                    { value: 'any', label: 'Any' },
                    { value: 'new', label: 'New' },
                    { value: 'preowned', label: 'Pre-owned' },
                  ]}
                />
              </Field>
            )}
            <Field label="Availability">
              <SegmentedControl
                size="sm"
                value={form.stock_filter}
                onChange={(stock_filter) => set('stock_filter', stock_filter)}
                options={[
                  { value: 'any', label: 'Any' },
                  { value: 'in_stock', label: 'In stock' },
                  { value: 'preorder', label: 'Pre-order' },
                ]}
              />
            </Field>
          </div>

          {form.condition !== 'new' && (
            <div className="space-y-3 rounded-control border border-line bg-raised p-3.5">
              <div>
                <p className="text-xs font-medium uppercase tracking-wide text-muted">
                  Minimum pre-owned condition
                </p>
                <p className="mt-1 text-xs leading-relaxed text-faint">
                  One product code often covers several graded copies at different prices. Setting
                  a minimum makes the target price apply to the cheapest copy that is actually good
                  enough, instead of to the cheapest copy overall.
                </p>
              </div>

              {(
                [
                  ['min_item_grade', 'Figure'],
                  ['min_box_grade', 'Box'],
                ] as const
              ).map(([key, label]) => (
                <div key={key}>
                  <span className="label mb-1">{label}</span>
                  <div className="flex flex-wrap gap-1.5">
                    <button
                      type="button"
                      onClick={() => set(key, '')}
                      className={clsx('chip', !form[key] && 'chip-active')}
                    >
                      Any
                    </button>
                    {GRADES.map((grade) => (
                      <button
                        key={grade}
                        type="button"
                        onClick={() => set(key, grade)}
                        className={clsx('chip', form[key] === grade && 'chip-active')}
                      >
                        {grade}
                        {form[key] === grade && (
                          <span className="text-faint">or better</span>
                        )}
                      </button>
                    ))}
                  </div>
                </div>
              ))}

              {form.kind === 'search' && (form.min_item_grade || form.min_box_grade) && (
                <p className="flex items-start gap-1.5 text-xs text-faint">
                  <Icon name="info" className="mt-0.5 h-3.5 w-3.5" />
                  Grades only appear on a product's own page, so a few candidates per check are
                  opened to read them. The rest are resolved on later checks.
                </p>
              )}
            </div>
          )}

          {form.kind === 'search' && (
            <Field
              label="Exclude words"
              hint="Comma separated. The single most effective way to stop a broad search from being noisy."
            >
              <input
                value={form.exclude}
                onChange={(e) => set('exclude', e.target.value)}
                className="field"
                placeholder="keychain, acrylic, tapestry, poster"
              />
            </Field>
          )}
        </div>
      )}

      {tab === 'when' && (
        <div className="space-y-5">
          <div>
            <span className="label">Compare against</span>
            <div className="grid gap-2 sm:grid-cols-2">
              {(
                [
                  {
                    value: 'listed' as const,
                    title: 'Shop price',
                    body: 'The number AmiAmi shows, in JPY. Simple and exactly what you see on the site.',
                    icon: 'yen' as const,
                  },
                  {
                    value: 'landed' as const,
                    title: 'Total incl. import',
                    body: `What it actually costs you in ${landedCurrency}, with shipping, duty and VAT estimated.`,
                    icon: 'box' as const,
                  },
                ]
              ).map((option) => (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => set('price_basis', option.value)}
                  className={clsx(
                    'rounded-control border p-3 text-left transition-all',
                    form.price_basis === option.value
                      ? 'border-accent bg-accent/8 shadow-glow'
                      : 'border-line bg-raised hover:border-faint',
                  )}
                >
                  <span className="flex items-center gap-2 text-sm font-medium">
                    <Icon name={option.icon} className="h-4 w-4 text-accent" />
                    {option.title}
                  </span>
                  <span className="mt-1 block text-xs leading-relaxed text-muted">
                    {option.body}
                  </span>
                </button>
              ))}
            </div>
            {form.price_basis === 'landed' && (
              <p className="mt-2 flex items-start gap-1.5 text-xs text-faint">
                <Icon name="info" className="mt-0.5 h-3.5 w-3.5" />
                Uses the shipping and customs settings from your cost profile.
              </p>
            )}
          </div>

          <Field
            label={`Target price (${currencyForTarget})`}
            hint="Alert when the price crosses below this. Leave empty to be told about every new match instead."
          >
            <div className="relative">
              <input
                type="number"
                min={0}
                step={form.price_basis === 'landed' ? 1 : 100}
                value={form.target_price}
                onChange={(e) => set('target_price', e.target.value)}
                className="field pr-16 text-lg font-semibold tabular-nums"
                placeholder={form.price_basis === 'landed' ? '90' : '12000'}
              />
              <span className="pointer-events-none absolute right-3.5 top-1/2 -translate-y-1/2 text-sm font-medium text-faint">
                {currencyForTarget}
              </span>
            </div>
            {seedItem?.price && form.price_basis === 'listed' && (
              <p className="mt-1.5 text-xs text-faint">
                Currently {money(seedItem.price, seedItem.currency ?? 'JPY')}
                {seedItem.landed &&
                  ` · ${money(seedItem.landed.total, seedItem.landed.currency)} landed`}
              </p>
            )}
          </Field>

          <div className="space-y-3 rounded-control border border-line bg-raised p-3.5">
            <div>
              <p className="text-xs font-medium uppercase tracking-wide text-muted">
                Alert me when
              </p>
              <p className="mt-1 text-xs leading-relaxed text-faint">
                Each switch is a separate reason to be told. An item can meet several at once
                and you still get one message, named after the most important of them, with the
                others listed underneath. Turning one off means that reason alone never wakes
                you; the item can still reach you through another.
              </p>
            </div>
            <Toggle
              checked={form.notify_on_price_below}
              onChange={(v) => set('notify_on_price_below', v)}
              label="The price crosses below my target"
              hint="Fires the moment it comes down across your target, not on every check while it sits below. Something already cheaper than your target when the watch was made is not news, so it stays quiet until it moves. Needs a target price."
              disabled={!form.target_price}
            />
            <Toggle
              checked={form.notify_on_restock}
              onChange={(v) => set('notify_on_restock', v)}
              label="Something comes back in stock"
              hint="Only when a listing this watch already knew about was out of stock and now is not. On a pre-owned product that means a fresh copy has been taken in, because AmiAmi deletes a copy the moment it sells rather than marking it gone."
            />
            <Toggle
              checked={form.notify_on_new_match}
              onChange={(v) => set('notify_on_new_match', v)}
              label="A new listing matches"
              hint="A listing that did not exist at the previous check, is actually buyable, and is within your target if you set one. This is the one that finds figures you were not already watching, so it is the switch to leave on for a search and to turn off for a single item you already know about."
            />
            <Field
              label="Also alert on any drop of at least"
              hint="A percentage off whatever it cost at the previous check. Independent of your target, so it catches a listing falling from far above it - useful for watching something drift down towards a price you would accept. Leave empty to ignore drops that stay above your target."
            >
              <div className="relative w-32">
                <input
                  type="number"
                  min={1}
                  max={99}
                  value={form.notify_on_price_drop_pct}
                  onChange={(e) => set('notify_on_price_drop_pct', e.target.value)}
                  className="field pr-7 tabular-nums"
                  placeholder="15"
                />
                <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-sm text-faint">
                  %
                </span>
              </div>
            </Field>
          </div>
        </div>
      )}

      {tab === 'how' && (
        <div className="space-y-5">
          <div>
            <span className="label">Check every</span>
            <div className="flex flex-wrap gap-2">
              {INTERVAL_PRESETS.map((preset) => (
                <Tooltip key={preset.seconds} content={preset.note}>
                  <button
                    type="button"
                    onClick={() => set('interval_seconds', preset.seconds)}
                    className={clsx(
                      'rounded-control border px-3 py-1.5 text-sm font-medium transition-all',
                      form.interval_seconds === preset.seconds
                        ? 'border-accent bg-accent/12 text-accent'
                        : 'border-line bg-raised text-muted hover:border-faint hover:text-ink',
                    )}
                  >
                    {preset.label}
                  </button>
                </Tooltip>
              ))}
            </div>
            {form.interval_seconds <= 60 && (
              <p className="mt-2 flex items-start gap-1.5 text-xs text-warning">
                <Icon name="alertTriangle" className="mt-0.5 h-3.5 w-3.5" />
                Very frequent checks share one global request budget with your other watches. Use
                this for a handful of grails, not everything.
              </p>
            )}
          </div>

          <Toggle
            checked={form.adaptive}
            onChange={(v) => set('adaptive', v)}
            label="Adaptive pacing"
            hint="Speeds up automatically when a match gets close to your target, and slows down when nothing is moving. Your interval is used as the baseline."
          />

          <Field
            label="Priority"
            hint="Higher priority watches jump the queue and are allowed to poll closer to the floor."
          >
            <SegmentedControl
              size="sm"
              value={String(form.priority)}
              onChange={(value) => set('priority', Number(value))}
              options={[
                { value: '0', label: 'Normal' },
                { value: '1', label: 'High' },
                { value: '2', label: 'Higher' },
                { value: '3', label: 'Grail' },
              ]}
            />
          </Field>

          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              label="Quiet period per item"
              hint="Stops one listing from alerting repeatedly."
            >
              <select
                value={form.cooldown_seconds}
                onChange={(e) => set('cooldown_seconds', Number(e.target.value))}
                className="field"
              >
                {[0, 300, 1800, 3600, 21600, 86400].map((seconds) => (
                  <option key={seconds} value={seconds}>
                    {seconds === 0 ? 'No cooldown' : duration(seconds)}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Maximum alerts per day" hint="A hard stop against runaway searches.">
              <input
                type="number"
                min={1}
                max={1000}
                value={form.max_alerts_per_day}
                onChange={(e) => set('max_alerts_per_day', Number(e.target.value))}
                className="field tabular-nums"
              />
            </Field>
          </div>

          <div className="space-y-3 rounded-control border border-line bg-raised p-3.5">
            <Toggle
              checked={form.quiet_enabled}
              onChange={(v) => set('quiet_enabled', v)}
              label="Quiet hours"
              hint="Alerts are still recorded, they just are not pushed."
            />
            {form.quiet_enabled && (
              <div className="space-y-3 pl-12">
                <div className="flex items-center gap-2">
                  <input
                    type="time"
                    value={form.quiet_start}
                    onChange={(e) => set('quiet_start', e.target.value)}
                    className="field w-32"
                  />
                  <span className="text-sm text-muted">to</span>
                  <input
                    type="time"
                    value={form.quiet_end}
                    onChange={(e) => set('quiet_end', e.target.value)}
                    className="field w-32"
                  />
                  <Badge>{user?.timezone ?? 'UTC'}</Badge>
                </div>
                <Toggle
                  checked={form.quiet_urgent}
                  onChange={(v) => set('quiet_urgent', v)}
                  label="Let urgent alerts through anyway"
                  hint="Target reached and back in stock still push. Everything else waits."
                />
              </div>
            )}
          </div>

          {channels && channels.length > 0 && (
            <Field
              label="Send to"
              hint="Nothing selected means every channel you marked as a default."
            >
              <div className="flex flex-wrap gap-2">
                {channels.map((channel) => {
                  const selected = form.channel_ids.includes(channel.id)
                  return (
                    <button
                      key={channel.id}
                      type="button"
                      onClick={() =>
                        set(
                          'channel_ids',
                          selected
                            ? form.channel_ids.filter((id) => id !== channel.id)
                            : [...form.channel_ids, channel.id],
                        )
                      }
                      className={clsx('chip', selected && 'chip-active')}
                    >
                      {selected && <Icon name="check" className="h-3 w-3" />}
                      {channel.name || channel.type}
                    </button>
                  )
                })}
              </div>
            </Field>
          )}

          {watch && (
            <Toggle
              checked={form.enabled}
              onChange={(v) => set('enabled', v)}
              label="Watch is active"
              hint="Pausing keeps the watch and its history, it just stops checking."
            />
          )}
        </div>
      )}
    </Modal>
  )
}
