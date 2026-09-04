import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { Channel, ChannelType, ChannelTypeInfo } from '@/api/types'
import { useAuth } from '@/lib/auth'
import { useToast } from '@/lib/toast'
import { relativeTime } from '@/lib/format'
import { Icon, type IconName } from './Icon'
import { Badge, Card, EmptyState, Field, Modal, Spinner, Toggle } from './ui'
import { TRIGGER_OPTIONS } from '@/lib/triggers'
import type { TriggerType } from '@/api/types'

const ALL_TRIGGERS = TRIGGER_OPTIONS.map((option) => option.value)

//: The two alerts that come with a percentage attached, and so the two a
//: channel can set its own bar for.
const THRESHOLD_TRIGGERS: TriggerType[] = ['price_drop', 'deal_radar']
import clsx from 'clsx'

const CHANNEL_ICON: Record<ChannelType, IconName> = {
  telegram: 'telegram',
  webpush: 'push',
  email: 'mail',
  discord: 'discord',
  ntfy: 'bell',
  gotify: 'bell',
  webhook: 'webhook',
}

const CHANNEL_BLURB: Record<ChannelType, string> = {
  telegram: 'Fastest option. Alerts land on your phone in about a second, with the product image.',
  webpush: 'Desktop and mobile notifications from the browser, even with the tab closed.',
  email: 'Best as a daily or weekly digest rather than an instant alert.',
  discord: 'Posts a rich embed into any channel via a webhook.',
  ntfy: 'Self-hostable push. Pairs well with a TrueNAS setup.',
  gotify: 'Self-hosted push server, if you already run one.',
  webhook: 'Raw JSON POST, for wiring into Home Assistant or anything else.',
}

/** Turn on browser push: ask permission, subscribe, hand the key to the server. */
async function enableWebPush(vapidKey: string, deviceName: string) {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    throw new Error('This browser does not support push notifications')
  }
  const permission = await Notification.requestPermission()
  if (permission !== 'granted') {
    throw new Error('Notification permission was declined')
  }

  const registration = await navigator.serviceWorker.register('/sw.js')
  await navigator.serviceWorker.ready

  const raw = atob(vapidKey.replace(/-/g, '+').replace(/_/g, '/'))
  const applicationServerKey = Uint8Array.from(raw, (char) => char.charCodeAt(0))

  const existing = await registration.pushManager.getSubscription()
  const subscription =
    existing ??
    (await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey }))

  return api.channels.subscribePush(subscription.toJSON(), deviceName)
}

function ChannelForm({
  spec,
  channel,
  onClose,
  onSaved,
}: {
  spec: ChannelTypeInfo
  channel: Channel | null
  onClose: () => void
  onSaved: () => void
}) {
  const toast = useToast()
  const [name, setName] = useState(channel?.name ?? spec.label)
  const [config, setConfig] = useState<Record<string, any>>(() => {
    const initial: Record<string, any> = {}
    for (const field of spec.fields) {
      if (field.type === 'hidden') continue
      const stored = channel?.config_preview?.[field.name]
      // Secrets come back redacted, so start those blank: leaving them empty
      // on save keeps whatever is already stored.
      initial[field.name] =
        field.type === 'password' ? '' : (stored ?? field.default ?? (field.type === 'boolean' ? false : ''))
    }
    return initial
  })
  const [isDefault, setIsDefault] = useState(channel?.is_default ?? true)
  const [sendDigest, setSendDigest] = useState(channel?.send_digest ?? false)
  // Which kinds this channel takes, and its own bar for the ones that carry a
  // percentage. Absent means "everything", so a channel set up before these
  // existed keeps behaving the way it did.
  const stored = channel?.config_preview ?? {}
  const [triggers, setTriggers] = useState<TriggerType[]>(
    Array.isArray(stored.triggers) && stored.triggers.length
      ? (stored.triggers as TriggerType[])
      : ALL_TRIGGERS,
  )
  const [thresholds, setThresholds] = useState<Record<string, number>>(
    (stored.thresholds as Record<string, number>) ?? {},
  )

  function toggleTrigger(kind: TriggerType, on: boolean) {
    setTriggers((current) =>
      on ? [...new Set([...current, kind])] : current.filter((k) => k !== kind),
    )
  }

  /** Everything the channel needs stored, secrets and routing together. */
  const fullConfig = () => ({
    ...config,
    // Written out in full rather than omitted when everything is ticked: an
    // absent list would silently start meaning "all" again the next time a
    // new kind of alert is added.
    triggers,
    thresholds,
  })
  const [detecting, setDetecting] = useState(false)
  const [chats, setChats] = useState<{ chat_id: string; name: string }[]>([])

  const save = useMutation({
    mutationFn: () =>
      channel
        ? api.channels.update(channel.id, {
            name,
            config: fullConfig(),
            is_default: isDefault,
            send_digest: sendDigest,
          })
        : api.channels.create({
            type: spec.type,
            name,
            config: fullConfig(),
            is_default: isDefault,
            send_digest: sendDigest,
          }),
    onSuccess: () => {
      toast.success(channel ? 'Channel updated' : 'Channel added')
      onSaved()
    },
    onError: (error) => toast.error('Could not save the channel', (error as Error).message),
  })

  async function detectTelegram() {
    setDetecting(true)
    try {
      const result = await api.channels.detectTelegram(String(config.bot_token ?? ''))
      setChats(result.detail ?? [])
      if (!result.ok) toast.error(result.message)
    } catch (error) {
      toast.error('Detection failed', (error as Error).message)
    } finally {
      setDetecting(false)
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={channel ? `Edit ${spec.label}` : `Add ${spec.label}`}
      subtitle={CHANNEL_BLURB[spec.type]}
      footer={
        <>
          <button onClick={onClose} className="btn-ghost">
            Cancel
          </button>
          <button onClick={() => save.mutate()} disabled={save.isPending} className="btn-primary">
            {save.isPending && <Spinner className="h-4 w-4" />}
            Save
          </button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Name">
          <input value={name} onChange={(e) => setName(e.target.value)} className="field" />
        </Field>

        {spec.fields
          .filter((field) => field.type !== 'hidden')
          .map((field) => (
            <Field
              key={field.name}
              label={field.label}
              hint={
                field.type === 'password' && channel
                  ? 'Leave empty to keep the value already stored.'
                  : field.help
              }
            >
              {field.type === 'boolean' ? (
                <Toggle
                  checked={Boolean(config[field.name])}
                  onChange={(value) => setConfig({ ...config, [field.name]: value })}
                  label={field.help ?? field.label}
                />
              ) : field.type === 'select' ? (
                <select
                  value={config[field.name] ?? field.default ?? ''}
                  onChange={(e) => setConfig({ ...config, [field.name]: e.target.value })}
                  className="field"
                >
                  {(field.options ?? []).map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              ) : field.type === 'textarea' ? (
                <textarea
                  value={config[field.name] ?? ''}
                  onChange={(e) => setConfig({ ...config, [field.name]: e.target.value })}
                  rows={3}
                  className="field font-mono text-xs"
                />
              ) : (
                <input
                  type={field.type === 'password' ? 'password' : field.type}
                  value={config[field.name] ?? ''}
                  onChange={(e) => setConfig({ ...config, [field.name]: e.target.value })}
                  className="field"
                  placeholder={field.type === 'password' && channel ? '••••••••' : undefined}
                  autoComplete="off"
                />
              )}
            </Field>
          ))}

        {spec.type === 'telegram' && (
          <div className="rounded-control border border-line bg-raised p-3">
            <p className="text-xs leading-relaxed text-muted">
              Send your bot any message from Telegram, then press detect and pick the chat. That
              saves hunting for your numeric chat ID.
            </p>
            <button
              onClick={detectTelegram}
              disabled={detecting || !config.bot_token}
              className="btn-ghost mt-2 text-xs"
            >
              {detecting ? <Spinner className="h-3 w-3" /> : <Icon name="search" className="h-3 w-3" />}
              Detect my chat
            </button>
            {chats.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {chats.map((chat) => (
                  <button
                    key={chat.chat_id}
                    onClick={() => setConfig({ ...config, chat_id: chat.chat_id })}
                    className={clsx('chip', config.chat_id === chat.chat_id && 'chip-active')}
                  >
                    {chat.name} · {chat.chat_id}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {/* What this channel is for. The same figure falling a fifth is worth
            a message on your phone and not worth an e-mail, so each channel
            takes the kinds it wants and sets its own bar on the two that
            carry a percentage. */}
        <div className="space-y-2 border-t border-line pt-4">
          <p className="text-sm font-medium">Send here</p>
          <div className="space-y-1.5">
            {TRIGGER_OPTIONS.map((option) => {
              const on = triggers.includes(option.value)
              const scaled = THRESHOLD_TRIGGERS.includes(option.value)
              return (
                <div key={option.value} className="space-y-1">
                  <Toggle
                    checked={on}
                    onChange={(next) => toggleTrigger(option.value, next)}
                    label={option.label}
                  />
                  {on && scaled && (
                    <div className="ml-8 flex items-center gap-2">
                      <span className="text-xs text-muted">only from</span>
                      <div className="relative w-20">
                        <input
                          type="number"
                          min={1}
                          max={95}
                          placeholder="any"
                          defaultValue={
                            thresholds[option.value] !== undefined
                              ? Math.round(thresholds[option.value] * 100)
                              : ''
                          }
                          onBlur={(e) => {
                            const raw = e.target.value.trim()
                            setThresholds((current) => {
                              const next = { ...current }
                              if (!raw) delete next[option.value]
                              else next[option.value] = Number(raw) / 100
                              return next
                            })
                          }}
                          className="field py-1 pr-6 text-xs tabular-nums"
                        />
                        <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-xs text-faint">
                          %
                        </span>
                      </div>
                      <span className="text-xs text-faint">
                        leave blank to take whatever your settings allow
                      </span>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
          {triggers.length === 0 && (
            <p className="text-xs text-warning">
              Nothing selected, so this channel will receive no alerts at all.
            </p>
          )}
        </div>

        <div className="space-y-3 border-t border-line pt-4">
          <Toggle
            checked={isDefault}
            onChange={setIsDefault}
            label="Use for new watches by default"
            hint="Watches with no explicit channel selection send here."
          />
          <Toggle
            checked={sendDigest}
            onChange={setSendDigest}
            label="Send periodic digests here"
            hint="A rolled-up summary on your chosen schedule, separate from instant alerts."
          />
        </div>

        {spec.docs_url && (
          <a
            href={spec.docs_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-xs text-accent hover:underline"
          >
            Setup guide
            <Icon name="external" className="h-3 w-3" />
          </a>
        )}
      </div>
    </Modal>
  )
}

export function ChannelManager() {
  const toast = useToast()
  const { config: appConfig } = useAuth()
  const queryClient = useQueryClient()
  const [adding, setAdding] = useState<ChannelTypeInfo | null>(null)
  const [editing, setEditing] = useState<{ spec: ChannelTypeInfo; channel: Channel } | null>(null)
  const [testing, setTesting] = useState<number | null>(null)
  const [pushBusy, setPushBusy] = useState(false)

  const channels = useQuery({ queryKey: ['channels'], queryFn: api.channels.list })
  const types = useQuery({ queryKey: ['channelTypes'], queryFn: api.channels.types })

  const refresh = () => void queryClient.invalidateQueries({ queryKey: ['channels'] })

  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      api.channels.update(id, { enabled }),
    onSuccess: refresh,
  })
  const remove = useMutation({
    mutationFn: (id: number) => api.channels.remove(id),
    onSuccess: () => {
      toast.success('Channel removed')
      refresh()
    },
  })

  async function test(id: number) {
    setTesting(id)
    try {
      await api.channels.test(id)
      toast.success('Test sent', 'Check the channel now.')
    } catch (error) {
      toast.error('Test failed', (error as Error).message)
    } finally {
      setTesting(null)
      refresh()
    }
  }

  async function addPush() {
    if (!appConfig?.vapid_public_key) {
      toast.error('Web Push is not configured', 'Ask the administrator to generate VAPID keys.')
      return
    }
    setPushBusy(true)
    try {
      await enableWebPush(appConfig.vapid_public_key, navigator.userAgent.slice(0, 60))
      toast.success('Browser push enabled on this device')
      refresh()
    } catch (error) {
      toast.error('Could not enable push', (error as Error).message)
    } finally {
      setPushBusy(false)
    }
  }

  return (
    <div className="space-y-5">
      <div>
        <h3 className="text-sm font-semibold">Your channels</h3>
        <p className="mt-0.5 text-sm text-muted">
          Alerts fan out to every enabled channel. Telegram and browser push are the ones that
          actually arrive in time.
        </p>
      </div>

      {channels.data?.length ? (
        <div className="space-y-2">
          {channels.data.map((channel) => {
            const spec = types.data?.find((t) => t.type === channel.type)
            return (
              <Card
                key={channel.id}
                className={clsx('flex items-center gap-3 p-3.5', !channel.enabled && 'opacity-60')}
              >
                <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-accent/12 text-accent">
                  <Icon name={CHANNEL_ICON[channel.type]} className="h-4.5 w-4.5" />
                </span>

                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="truncate text-sm font-medium">{channel.name || spec?.label}</p>
                    {channel.is_default && <Badge tone="accent">Default</Badge>}
                    {channel.send_digest && <Badge tone="info">Digest</Badge>}
                    {!channel.enabled && <Badge tone="danger">Off</Badge>}
                  </div>
                  {channel.last_error ? (
                    <p className="mt-0.5 truncate text-xs text-danger">{channel.last_error}</p>
                  ) : (
                    <p className="mt-0.5 text-xs text-faint">
                      {channel.last_used_at
                        ? `Last used ${relativeTime(channel.last_used_at)}`
                        : 'Never used yet'}
                    </p>
                  )}
                </div>

                <div className="flex shrink-0 gap-1.5">
                  <button
                    onClick={() => test(channel.id)}
                    disabled={testing === channel.id}
                    className="btn-ghost px-2.5"
                    title="Send a test"
                  >
                    {testing === channel.id ? <Spinner /> : <Icon name="play" />}
                  </button>
                  <button
                    onClick={() => toggle.mutate({ id: channel.id, enabled: !channel.enabled })}
                    className="btn-ghost px-2.5"
                    title={channel.enabled ? 'Disable' : 'Enable'}
                  >
                    <Icon name={channel.enabled ? 'pause' : 'play'} />
                  </button>
                  {spec && channel.type !== 'webpush' && (
                    <button
                      onClick={() => setEditing({ spec, channel })}
                      className="btn-ghost px-2.5"
                      title="Edit"
                    >
                      <Icon name="edit" />
                    </button>
                  )}
                  <button
                    onClick={() => remove.mutate(channel.id)}
                    className="btn-ghost px-2.5 text-danger"
                    title="Remove"
                  >
                    <Icon name="trash" />
                  </button>
                </div>
              </Card>
            )
          })}
        </div>
      ) : (
        <EmptyState
          icon="bell"
          title="No notification channels yet"
          body="Without a channel, alerts only appear inside this app. Add Telegram or browser push so they reach you when the tab is closed."
        />
      )}

      <div>
        <h3 className="mb-2 text-sm font-semibold">Add a channel</h3>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {(types.data ?? []).map((spec) => (
            <button
              key={spec.type}
              disabled={!spec.available}
              onClick={() => (spec.type === 'webpush' ? void addPush() : setAdding(spec))}
              className={clsx(
                'flex items-start gap-3 rounded-card border border-line bg-surface p-3 text-left transition-all',
                spec.available
                  ? 'hover:-translate-y-0.5 hover:border-accent/40 hover:shadow-pop'
                  : 'cursor-not-allowed opacity-50',
              )}
            >
              <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-raised text-muted">
                {spec.type === 'webpush' && pushBusy ? (
                  <Spinner className="h-4 w-4" />
                ) : (
                  <Icon name={CHANNEL_ICON[spec.type]} className="h-4 w-4" />
                )}
              </span>
              <span className="min-w-0">
                <span className="block text-sm font-medium">{spec.label}</span>
                <span className="mt-0.5 block text-xs leading-relaxed text-muted">
                  {spec.available ? CHANNEL_BLURB[spec.type] : spec.unavailable_reason}
                </span>
              </span>
            </button>
          ))}
        </div>
      </div>

      {adding && (
        <ChannelForm
          spec={adding}
          channel={null}
          onClose={() => setAdding(null)}
          onSaved={() => {
            setAdding(null)
            refresh()
          }}
        />
      )}
      {editing && (
        <ChannelForm
          spec={editing.spec}
          channel={editing.channel}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            refresh()
          }}
        />
      )}
    </div>
  )
}
