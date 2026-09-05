import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { CostProfile, CostProfilePreview, ShippingService } from '@/api/types'
import { Blocklist } from '@/components/Blocklist'
import { ChannelManager } from '@/components/ChannelManager'
import { Icon } from '@/components/Icon'
import { Card, Field, SegmentedControl, Spinner, Toggle } from '@/components/ui'
import { useAuth } from '@/lib/auth'
import { useTheme, THEMES } from '@/lib/theme'
import { useToast } from '@/lib/toast'
import { grams, money } from '@/lib/format'
import clsx from 'clsx'

const CURRENCIES = ['EUR', 'USD', 'GBP', 'CHF', 'SEK', 'NOK', 'DKK', 'PLN', 'CZK', 'CAD', 'AUD', 'JPY']

const COUNTRY_PRESETS: Record<string, Partial<CostProfile>> = {
  DE: { vat_rate: 0.19, duty_rate: 0.047, duty_free_threshold: 150, customs_handling_fee: 6, shipping_zone: 'zone3' },
  AT: { vat_rate: 0.2, duty_rate: 0.047, duty_free_threshold: 150, customs_handling_fee: 5, shipping_zone: 'zone3' },
  CH: { vat_rate: 0.081, duty_rate: 0, duty_free_threshold: 0, customs_handling_fee: 16, shipping_zone: 'zone3' },
  NL: { vat_rate: 0.21, duty_rate: 0.047, duty_free_threshold: 150, customs_handling_fee: 13, shipping_zone: 'zone3' },
  FR: { vat_rate: 0.2, duty_rate: 0.047, duty_free_threshold: 150, customs_handling_fee: 8, shipping_zone: 'zone3' },
  GB: { vat_rate: 0.2, duty_rate: 0.047, duty_free_threshold: 135, customs_handling_fee: 12, shipping_zone: 'zone3' },
  US: { vat_rate: 0, duty_rate: 0, duty_free_threshold: 800, customs_handling_fee: 0, shipping_zone: 'zone4' },
}

function AppearanceTab() {
  const { theme, setTheme, mode, setMode, cardShape, setCardShape } = useTheme()
  const { user, patchUser } = useAuth()

  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.auth.updateMe(body),
    onSuccess: (updated) => patchUser(updated),
  })

  // Persist the choice so a second device inherits it.
  useEffect(() => {
    if (!user) return
    if (user.theme !== theme || user.color_mode !== mode) {
      save.mutate({ theme, color_mode: mode })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [theme, mode])

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-sm font-semibold">Skin</h3>
        <p className="mt-0.5 text-sm text-muted">Two complete looks. Pick whichever you enjoy.</p>
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          {THEMES.map((entry) => (
            <button
              key={entry.id}
              onClick={() => setTheme(entry.id)}
              className={clsx(
                'rounded-card border p-4 text-left transition-all',
                theme === entry.id
                  ? 'border-accent shadow-glow'
                  : 'border-line bg-surface hover:border-faint',
              )}
            >
              <div className="flex items-center gap-2.5">
                <span className="flex -space-x-1.5">
                  {entry.swatch.map((colour) => (
                    <span
                      key={colour}
                      className="h-5 w-5 rounded-full ring-2 ring-surface"
                      style={{ background: colour }}
                    />
                  ))}
                </span>
                <span className="text-sm font-semibold">{entry.label}</span>
                {theme === entry.id && <Icon name="check" className="ml-auto h-4 w-4 text-accent" />}
              </div>
              <p className="mt-2 text-xs leading-relaxed text-muted">{entry.blurb}</p>
            </button>
          ))}
        </div>
      </div>

      <Field label="Brightness">
        <SegmentedControl
          value={mode}
          onChange={setMode}
          options={[
            { value: 'dark', label: 'Dark', icon: 'moon' },
            { value: 'light', label: 'Light', icon: 'sun' },
            { value: 'system', label: 'System', icon: 'monitor' },
          ]}
        />
      </Field>

      <Field
        label="Card shape"
        hint="AmiAmi's photos are square. Square shows the whole picture; portrait crops it but fits more figures on a screen."
      >
        <SegmentedControl
          value={cardShape}
          onChange={setCardShape}
          options={[
            { value: 'portrait', label: 'Portrait', icon: 'grid' },
            { value: 'square', label: 'Square', icon: 'box' },
          ]}
        />
      </Field>
    </div>
  )
}

function CostTab() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const profile = useQuery({ queryKey: ['costProfile'], queryFn: api.auth.costProfile })
  const [draft, setDraft] = useState<CostProfile | null>(null)
  const current = draft ?? profile.data ?? null

  const save = useMutation({
    mutationFn: (body: Partial<CostProfile>) => api.auth.updateCostProfile(body),
    onSuccess: () => {
      toast.success('Cost profile saved', 'Landed prices now use these numbers.')
      void queryClient.invalidateQueries({ queryKey: ['costProfile'] })
      void queryClient.invalidateQueries({ queryKey: ['search'] })
      void queryClient.invalidateQueries({ queryKey: ['item'] })
      setDraft(null)
    },
    onError: (error) => toast.error('Could not save', (error as Error).message),
  })

  const shipping = useQuery({
    queryKey: ['shippingOptions'],
    queryFn: api.auth.shippingOptions,
    staleTime: Infinity,
  })

  // The worked example is priced by the estimator itself rather than
  // approximated here, because a rate chart quoted in yen cannot be guessed
  // at in the browser. Settle first so typing does not fire a request a key.
  const [settled, setSettled] = useState<CostProfile | null>(null)
  useEffect(() => {
    const timer = setTimeout(() => setSettled(current), 350)
    return () => clearTimeout(timer)
  }, [current])
  const preview = useQuery({
    queryKey: ['costPreview', settled],
    queryFn: () => api.auth.previewCostProfile(settled ?? {}),
    enabled: settled !== null,
    placeholderData: (previous?: CostProfilePreview) => previous,
  })

  if (!current) return <Spinner className="h-5 w-5" />
  const set = (changes: Partial<CostProfile>) => setDraft({ ...current, ...changes })

  const currency = user?.display_currency ?? 'EUR'

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-sm font-semibold">Landed cost</h3>
        <p className="mt-0.5 max-w-2xl text-sm text-muted">
          The price AmiAmi shows is not the price you pay. These settings drive every "landed"
          figure in the app, and any watch whose target is set on the total instead of the shop
          price.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <Field label="Country" hint="Picking one fills in sensible defaults.">
          <select
            value={current.country}
            onChange={(e) =>
              set({ country: e.target.value, ...(COUNTRY_PRESETS[e.target.value] ?? {}) })
            }
            className="field"
          >
            {Object.keys(COUNTRY_PRESETS).map((code) => (
              <option key={code} value={code}>
                {code}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Import VAT" hint="19% in Germany.">
          <div className="relative">
            <input
              type="number"
              step={0.001}
              min={0}
              max={1}
              value={current.vat_rate}
              onChange={(e) => set({ vat_rate: Number(e.target.value) })}
              className="field pr-12 tabular-nums"
            />
            <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-xs text-faint">
              {(current.vat_rate * 100).toFixed(1)}%
            </span>
          </div>
        </Field>
        <Field label="Customs duty" hint="Figures sit around 4.7% into the EU.">
          <div className="relative">
            <input
              type="number"
              step={0.001}
              min={0}
              max={1}
              value={current.duty_rate}
              onChange={(e) => set({ duty_rate: Number(e.target.value) })}
              className="field pr-12 tabular-nums"
            />
            <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-xs text-faint">
              {(current.duty_rate * 100).toFixed(1)}%
            </span>
          </div>
        </Field>
        <Field label="Duty-free below" hint={`Goods value in ${currency}. 150 in the EU.`}>
          <input
            type="number"
            min={0}
            value={current.duty_free_threshold}
            onChange={(e) => set({ duty_free_threshold: Number(e.target.value) })}
            className="field tabular-nums"
          />
        </Field>
        <Field label="Carrier handling fee" hint="DHL charges about 6 EUR to clear a parcel.">
          <input
            type="number"
            min={0}
            step={0.5}
            value={current.customs_handling_fee}
            onChange={(e) => set({ customs_handling_fee: Number(e.target.value) })}
            className="field tabular-nums"
          />
        </Field>
        <Field label="Card FX spread" hint="What your card adds on top of the mid-market rate.">
          <div className="relative">
            <input
              type="number"
              step={0.001}
              min={0}
              max={0.2}
              value={current.fx_markup}
              onChange={(e) => set({ fx_markup: Number(e.target.value) })}
              className="field pr-12 tabular-nums"
            />
            <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-xs text-faint">
              {(current.fx_markup * 100).toFixed(1)}%
            </span>
          </div>
        </Field>
      </div>

      <div className="space-y-4 border-t border-line pt-5">
        <h4 className="text-sm font-semibold">Shipping</h4>
        <SegmentedControl
          value={current.shipping_mode}
          onChange={(shipping_mode) => set({ shipping_mode })}
          options={[
            { value: 'amiami', label: 'AmiAmi rates' },
            { value: 'table', label: 'By weight' },
            { value: 'flat', label: 'Flat rate' },
            { value: 'none', label: 'Exclude' },
          ]}
        />

        {current.shipping_mode === 'amiami' && (
          <div className="space-y-4">
            <p className="text-xs text-muted">
              AmiAmi's own published rate charts, converted from yen at the live exchange rate.
              The shop bills by zone rather than by country, and small packet stops at 2&nbsp;kg.
            </p>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Shipping zone" hint="Set for you when you pick a country.">
                <select
                  value={current.shipping_zone}
                  onChange={(e) => set({ shipping_zone: e.target.value as CostProfile['shipping_zone'] })}
                  className="field"
                >
                  {(shipping.data?.zones ?? []).map((zone) => (
                    <option key={zone.value} value={zone.value}>
                      {zone.label}
                    </option>
                  ))}
                </select>
              </Field>
              <Field
                label="Service"
                hint="Surface parcel is sea mail: cheap, but one to three months in transit."
              >
                <select
                  value={current.shipping_service}
                  onChange={(e) => set({ shipping_service: e.target.value as ShippingService })}
                  className="field"
                >
                  <option value="auto_air">Cheapest by air</option>
                  <option value="auto">Cheapest of any kind</option>
                  {(shipping.data?.services ?? []).map((service) => (
                    <option key={service.value} value={service.value}>
                      {service.label}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
          </div>
        )}

        {current.shipping_mode === 'flat' && (
          <Field label={`Flat shipping (${currency})`}>
            <input
              type="number"
              min={0}
              value={current.shipping_flat}
              onChange={(e) => set({ shipping_flat: Number(e.target.value) })}
              className="field max-w-[12rem] tabular-nums"
            />
          </Field>
        )}

        {current.shipping_mode === 'table' && (
          <div className="space-y-2">
            <p className="text-xs text-muted">
              What your carrier charges per weight bracket. Weight is estimated from the product
              size or, failing that, from the item type.
            </p>
            {current.shipping_table.map((bracket, index) => (
              <div key={index} className="flex items-center gap-2">
                <span className="text-xs text-faint">up to</span>
                <input
                  type="number"
                  value={bracket.max_grams}
                  onChange={(e) => {
                    const next = [...current.shipping_table]
                    next[index] = { ...bracket, max_grams: Number(e.target.value) }
                    set({ shipping_table: next })
                  }}
                  className="field w-28 tabular-nums"
                />
                <span className="text-xs text-faint">g costs</span>
                <input
                  type="number"
                  step={0.5}
                  value={bracket.cost}
                  onChange={(e) => {
                    const next = [...current.shipping_table]
                    next[index] = { ...bracket, cost: Number(e.target.value) }
                    set({ shipping_table: next })
                  }}
                  className="field w-28 tabular-nums"
                />
                <span className="text-xs text-faint">{currency}</span>
                <button
                  onClick={() =>
                    set({ shipping_table: current.shipping_table.filter((_, i) => i !== index) })
                  }
                  className="btn-quiet px-2 py-1 text-danger"
                >
                  <Icon name="trash" className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
            <button
              onClick={() =>
                set({
                  shipping_table: [
                    ...current.shipping_table,
                    { max_grams: 2000, cost: 30 },
                  ],
                })
              }
              className="btn-ghost text-xs"
            >
              <Icon name="plus" className="h-3 w-3" />
              Add bracket
            </button>
          </div>
        )}

        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            label="Default weight when unknown"
            hint="Used when neither the spec sheet nor the item type gives a clue."
          >
            <input
              type="number"
              min={1}
              value={current.default_weight_grams}
              onChange={(e) => set({ default_weight_grams: Number(e.target.value) })}
              className="field tabular-nums"
            />
          </Field>
          <Field
            label="Packaging weight"
            hint="Box and padding, added on top of every shipment. The carrier weighs the parcel, not the figure."
          >
            <input
              type="number"
              min={0}
              max={10000}
              value={current.packaging_grams}
              onChange={(e) => set({ packaging_grams: Number(e.target.value) })}
              className="field tabular-nums"
            />
          </Field>
        </div>

        <Field
          label="Weight estimate"
          hint="Real parcels for figures of much the same size arrive anywhere between 1.0 and 1.5 kg, so no table gets everyone right. Nudge the whole estimate if your postage keeps coming out wrong."
        >
          <div className="flex items-center gap-3">
            <input
              type="range"
              min={0.5}
              max={2}
              step={0.05}
              value={current.weight_scale}
              onChange={(e) => set({ weight_scale: Number(e.target.value) })}
              className="flex-1 accent-accent"
            />
            <span className="w-16 text-right font-mono text-sm tabular-nums">
              {current.weight_scale.toFixed(2)}&times;
            </span>
          </div>
        </Field>

        <Toggle
          checked={current.consolidate_shipping}
          onChange={(consolidate_shipping) => set({ consolidate_shipping })}
          label="I usually combine several items into one parcel"
          hint="Halves the shipping share attributed to each item."
        />
      </div>

      <Card className="p-4">
        <p className="text-xs font-semibold uppercase tracking-wide text-muted">
          Worked example
          {preview.data
            ? `: a ${money(preview.data.sample_price, 'JPY')} 1/7 figure shipping at ${grams(
                preview.data.weight_grams,
              )}`
            : ''}
        </p>
        {preview.data?.breakdown ? (
          <>
            <div className="mt-2 space-y-1 text-sm">
              {[
                ['Goods, converted', preview.data.breakdown.goods],
                ['Shipping', preview.data.breakdown.shipping],
                ['Customs duty', preview.data.breakdown.duty],
                ['Import VAT', preview.data.breakdown.vat],
                ['Handling fee', preview.data.breakdown.handling],
              ].map(([label, value]) => (
                <div key={label as string} className="flex justify-between">
                  <span className="text-muted">{label as string}</span>
                  <span className="tabular-nums">{money(value as number, currency)}</span>
                </div>
              ))}
              <div className="flex justify-between border-t border-line pt-1.5 font-semibold">
                <span>You pay</span>
                <span className="tabular-nums">
                  {money(preview.data.breakdown.total, currency)}
                </span>
              </div>
            </div>
            {preview.data.breakdown.notes.length > 0 && (
              <ul className="mt-3 space-y-1 border-t border-line pt-2 text-xs text-faint">
                {preview.data.breakdown.notes.map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            )}
          </>
        ) : (
          <p className="mt-2 text-sm text-muted">
            {preview.isFetching
              ? 'Pricing your settings…'
              : 'No exchange rate yet, so the example cannot be priced.'}
          </p>
        )}
      </Card>

      {draft && (
        <div className="sticky bottom-4 flex items-center gap-2 rounded-card border border-accent/40 bg-surface p-3 shadow-pop">
          <p className="flex-1 text-sm">You have unsaved changes.</p>
          <button onClick={() => setDraft(null)} className="btn-ghost">
            Discard
          </button>
          <button
            onClick={() => save.mutate(draft)}
            disabled={save.isPending}
            className="btn-primary"
          >
            {save.isPending && <Spinner className="h-4 w-4" />}
            Save
          </button>
        </div>
      )}
    </div>
  )
}

function AccountTab() {
  const { user, patchUser } = useAuth()
  const toast = useToast()
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')

  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.auth.updateMe(body),
    onSuccess: (updated) => {
      patchUser(updated)
      toast.success('Saved')
    },
  })

  const changePassword = useMutation({
    mutationFn: () =>
      api.auth.changePassword({ current_password: currentPassword, new_password: newPassword }),
    onSuccess: (result) => {
      toast.success(result.message)
      setCurrentPassword('')
      setNewPassword('')
    },
    onError: (error) => toast.error('Could not change the password', (error as Error).message),
  })

  const digest = (user?.prefs?.digest ?? {}) as Record<string, any>
  const radar = (user?.prefs?.deal_radar ?? {}) as Record<string, any>
  const drop = (user?.prefs?.price_drop ?? {}) as Record<string, any>

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Display currency" hint="Every landed price is shown in this currency.">
          <select
            value={user?.display_currency}
            onChange={(e) => save.mutate({ display_currency: e.target.value })}
            className="field"
          >
            {CURRENCIES.map((code) => (
              <option key={code} value={code}>
                {code}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Time zone" hint="Used for quiet hours and digest timing.">
          <input
            defaultValue={user?.timezone}
            onBlur={(e) => save.mutate({ timezone: e.target.value })}
            className="field"
            placeholder="Europe/Berlin"
          />
        </Field>
      </div>

      <div className="space-y-3 border-t border-line pt-5">
        <h4 className="text-sm font-semibold">Digest</h4>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Frequency">
            <select
              value={digest.frequency ?? 'daily'}
              onChange={(e) =>
                save.mutate({ prefs: { digest: { ...digest, frequency: e.target.value } } })
              }
              className="field"
            >
              <option value="off">Off</option>
              <option value="daily">Daily</option>
              <option value="weekly">Weekly</option>
            </select>
          </Field>
          <Field label="Send at" hint="Local hour, in your time zone.">
            <input
              type="number"
              min={0}
              max={23}
              defaultValue={digest.hour ?? 9}
              onBlur={(e) =>
                save.mutate({ prefs: { digest: { ...digest, hour: Number(e.target.value) } } })
              }
              className="field max-w-[8rem] tabular-nums"
            />
          </Field>
        </div>
        <button onClick={() => void api.channels.sendDigest().then((r) => toast.success(r.message))} className="btn-ghost text-sm">
          <Icon name="mail" />
          Send one now
        </button>
      </div>

      {/* One section, because from outside these looked like the same
          feature listed twice: both about a wishlisted figure, both with a
          percentage, both saying 25. They answer different questions, and
          the only way to see that is to put them side by side and name the
          question each one asks. */}
      <div className="space-y-4 border-t border-line pt-5">
        <div>
          <h4 className="text-sm font-semibold">Bargains on your wishlist</h4>
          <p className="mt-1 text-sm text-muted">
            Two different questions, each with its own switch. Which channels these reach, and
            how steep a fall each channel bothers you about, is set on the channel itself.
          </p>
        </div>

        <div className="space-y-3 rounded-control border border-line bg-raised p-3">
          <Toggle
            checked={drop.enabled ?? false}
            onChange={(enabled) => save.mutate({ prefs: { price_drop: { ...drop, enabled } } })}
            label="Cheaper than it was"
          />
          <p className="text-xs leading-relaxed text-muted">
            Compares against what the figure last cost, across both its listings. This is what
            catches a used copy appearing under a new listing you could not buy &mdash; the
            usual way a wishlist gets built, since when you first want a figure there is
            rarely a used one to save. For a figure that was already sold out when you saved
            it, the comparison is against the price it last sold at, which may be months old.
            Checked every six hours, so a slide of a few per cent a day will not add up to a
            message.
          </p>
          {(drop.enabled ?? false) && (
            <Field label="From at least">
              <div className="relative max-w-[8rem]">
                <input
                  type="number"
                  min={5}
                  max={90}
                  defaultValue={Math.round((drop.percent ?? 0.25) * 100)}
                  onBlur={(e) =>
                    save.mutate({
                      prefs: {
                        price_drop: { ...drop, percent: Number(e.target.value) / 100 },
                      },
                    })
                  }
                  className="field pr-7 tabular-nums"
                />
                <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-sm text-faint">
                  %
                </span>
              </div>
            </Field>
          )}
        </div>

        <div className="space-y-3 rounded-control border border-line bg-raised p-3">
          <Toggle
            checked={radar.enabled ?? true}
            onChange={(enabled) => save.mutate({ prefs: { deal_radar: { ...radar, enabled } } })}
            label="Cheap for this figure"
          />
          <p className="text-xs leading-relaxed text-muted">
            Compares against the figure&rsquo;s own tracked price history rather than against
            yesterday. It needs a few recorded prices before it can say anything, so it stays
            quiet on figures that have only just appeared &mdash; which is exactly where the
            other one speaks up.
          </p>
          {(radar.enabled ?? true) && (
            <Field label="From at least">
              <div className="relative max-w-[8rem]">
                <input
                  type="number"
                  min={5}
                  max={90}
                  defaultValue={Math.round((radar.discount ?? 0.25) * 100)}
                  onBlur={(e) =>
                    save.mutate({
                      prefs: { deal_radar: { ...radar, discount: Number(e.target.value) / 100 } },
                    })
                  }
                  className="field pr-7 tabular-nums"
                />
                <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-sm text-faint">
                  %
                </span>
              </div>
            </Field>
          )}
        </div>

        <button
          onClick={() => void api.scanDeals().then((r) => toast.success(r.message))}
          className="btn-ghost text-sm"
        >
          <Icon name="fire" />
          Check both now
        </button>
      </div>

      <div className="space-y-3 border-t border-line pt-5">
        <h4 className="text-sm font-semibold">Password</h4>
        <div className="grid max-w-md gap-3">
          <input
            type="password"
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
            placeholder="Current password"
            className="field"
            autoComplete="current-password"
          />
          <input
            type="password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            placeholder="New password, at least 10 characters"
            className="field"
            autoComplete="new-password"
          />
          <button
            onClick={() => changePassword.mutate()}
            disabled={!currentPassword || newPassword.length < 10 || changePassword.isPending}
            className="btn-primary w-fit"
          >
            {changePassword.isPending && <Spinner className="h-4 w-4" />}
            Change password
          </button>
          <p className="text-xs text-faint">Changing it signs out every other session.</p>
        </div>
      </div>
    </div>
  )
}

/**
 * Things you never want to see.
 *
 * It shares the cost profile's storage, which is why it started out on that
 * tab - a filing decision, not a reason. Nothing here has anything to do with
 * shipping or customs, so it gets its own place.
 */
function BlocklistTab() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const profile = useQuery({ queryKey: ['costProfile'], queryFn: api.auth.costProfile })
  const [draft, setDraft] = useState<CostProfile | null>(null)
  const current = draft ?? profile.data ?? null

  const save = useMutation({
    mutationFn: (body: Partial<CostProfile>) => api.auth.updateCostProfile(body),
    onSuccess: () => {
      toast.success('Blocklist saved', 'Search and Discover will skip these from now on.')
      void queryClient.invalidateQueries({ queryKey: ['costProfile'] })
      void queryClient.invalidateQueries({ queryKey: ['search'] })
      void queryClient.invalidateQueries({ queryKey: ['discover'] })
      setDraft(null)
    },
    onError: (error) => toast.error('Could not save', (error as Error).message),
  })

  if (!current) return <Spinner className="h-5 w-5" />
  const set = (changes: Partial<CostProfile>) => setDraft({ ...current, ...changes })

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-sm font-semibold">Never show me</h3>
        <p className="mt-0.5 max-w-2xl text-sm text-muted">
          Applied to search results and the discovery feed. Your watches are left alone: you
          asked for those by name, and hiding their results would be a silent failure rather
          than a tidy list. A single search can still look past this list without emptying it.
        </p>
      </div>

      <Field
        label="Words in the product name"
        hint="Matched anywhere in the name, so 'nendoroid' hides the nendoroids without touching a scale figure by the same maker."
      >
        <Blocklist
          values={current.blocked_terms}
          onChange={(blocked_terms) => set({ blocked_terms })}
          placeholder="nendoroid"
          empty="Nothing blocked by name."
        />
      </Field>

      <Field
        label="MyFigureCollection tags"
        hint="Tag slugs as they appear on MFC, for instance 'chibi'. Only reaches figures that have been cross-referenced, so it is the weaker of the two."
      >
        <Blocklist
          values={current.blocked_tags}
          onChange={(blocked_tags) => set({ blocked_tags })}
          placeholder="chibi"
          empty="Nothing blocked by tag."
        />
      </Field>

      {draft && (
        <div className="sticky bottom-4 flex items-center gap-2 rounded-card border border-accent/40 bg-surface p-3 shadow-pop">
          <span className="text-sm text-muted">Unsaved changes</span>
          <button onClick={() => setDraft(null)} className="btn-quiet ml-auto text-sm">
            Discard
          </button>
          <button
            onClick={() => save.mutate(draft)}
            disabled={save.isPending}
            className="btn-primary text-sm"
          >
            {save.isPending ? 'Saving…' : 'Save'}
          </button>
        </div>
      )}
    </div>
  )
}


const TABS = [
  { value: 'notifications', label: 'Notifications', icon: 'bell' as const },
  { value: 'cost', label: 'Landed cost', icon: 'box' as const },
  { value: 'blocklist', label: 'Blocklist', icon: 'filter' as const },
  { value: 'appearance', label: 'Appearance', icon: 'sparkle' as const },
  { value: 'account', label: 'Account', icon: 'user' as const },
]

export function SettingsPage() {
  const [tab, setTab] = useState('notifications')

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="mt-1 text-sm text-muted">
          Everything here is per account, so a shared instance stays personal.
        </p>
      </header>

      <SegmentedControl value={tab} onChange={setTab} options={TABS} />

      <Card className="p-5">
        {tab === 'notifications' && <ChannelManager />}
        {tab === 'cost' && <CostTab />}
        {tab === 'blocklist' && <BlocklistTab />}
        {tab === 'appearance' && <AppearanceTab />}
        {tab === 'account' && <AccountTab />}
      </Card>
    </div>
  )
}
