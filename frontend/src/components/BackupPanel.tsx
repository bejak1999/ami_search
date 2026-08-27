import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import { Icon } from '@/components/Icon'
import { Card, Modal, Toggle } from '@/components/ui'
import { useToast } from '@/lib/toast'

/**
 * Taking the instance with you, and putting it back.
 *
 * The database is where the irreplaceable half lives: years of price history
 * and every listing AmiAmi has since deleted. It is small. The photos are
 * gigabytes and only matter for listings that no longer exist anywhere else,
 * so they are a separate choice rather than an assumption.
 *
 * Restoring is destructive, so it is deliberately two steps: the file is read
 * and described first, and only then is there something to confirm.
 */

function bytes(value: number | null | undefined): string {
  if (!value) return '—'
  const units = ['B', 'kB', 'MB', 'GB']
  let size = value
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size.toFixed(size < 10 && unit > 0 ? 1 : 0)} ${units[unit]}`
}

type Manifest = {
  created_at?: string
  database_bytes?: number
  images?: number
  image_bytes?: number
  includes_images?: boolean
  includes_secrets?: boolean
  archive_bytes?: number
}

export function BackupPanel() {
  const toast = useToast()
  const queryClient = useQueryClient()

  const [includeImages, setIncludeImages] = useState(false)
  const [includeSecrets, setIncludeSecrets] = useState(true)
  const [restoreImages, setRestoreImages] = useState(true)
  const [pending, setPending] = useState<{ file: File; manifest: Manifest } | null>(null)

  const backupInput = useRef<HTMLInputElement>(null)
  const configInput = useRef<HTMLInputElement>(null)

  const downloadBackup = useMutation({
    mutationFn: () => api.admin.downloadBackup(includeImages, includeSecrets),
    onSuccess: () => toast.success('Backup saved', 'Check your downloads folder.'),
    onError: (error) => toast.error('Backup failed', (error as Error).message),
  })

  const exportConfig = useMutation({
    mutationFn: () => api.admin.exportConfig(includeSecrets),
    onSuccess: () => toast.success('Settings saved', 'Check your downloads folder.'),
    onError: (error) => toast.error('Export failed', (error as Error).message),
  })

  const inspect = useMutation({
    mutationFn: (file: File) => api.admin.inspectBackup(file),
    onError: (error) => toast.error('That file cannot be read', (error as Error).message),
  })

  const restore = useMutation({
    mutationFn: (file: File) => api.admin.restoreBackup(file, restoreImages),
    onSuccess: (result) => {
      toast.success('Restored', result.message)
      setPending(null)
      void queryClient.invalidateQueries()
    },
    onError: (error) => toast.error('Restore failed', (error as Error).message),
  })

  const importConfig = useMutation({
    mutationFn: (file: File) => api.admin.importConfig(file),
    onSuccess: (result) => {
      const skipped = (result.detail as { skipped?: string[] } | undefined)?.skipped ?? []
      toast.success('Settings applied', result.message)
      skipped.forEach((note) => toast.error('Skipped', note))
      void queryClient.invalidateQueries()
    },
    onError: (error) => toast.error('Import failed', (error as Error).message),
  })

  async function pickBackup(file: File | undefined) {
    if (!file) return
    const result = await inspect.mutateAsync(file).catch(() => null)
    if (result) setPending({ file, manifest: (result.detail as Manifest) ?? {} })
  }

  const manifest = pending?.manifest

  return (
    <Card className="p-4">
      <div className="mb-3">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <Icon name="download" className="h-4 w-4 text-accent" />
          Backup and restore
        </h3>
        <p className="mt-0.5 max-w-2xl text-xs text-muted">
          The database holds every price this instance has ever recorded, including listings
          AmiAmi has since deleted. It is the half that cannot be rebuilt, and it is small.
          Photos are optional because they run to gigabytes.
        </p>
      </div>

      <div className="space-y-3 border-t border-line pt-3">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-faint">
          Download a copy
        </h4>
        <Toggle
          checked={includeImages}
          onChange={setIncludeImages}
          label="Include product photos"
          hint="Much larger, but the photos of sold pre-owned listings exist nowhere else once the shop drops them."
        />
        <Toggle
          checked={includeSecrets}
          onChange={setIncludeSecrets}
          label="Include notification credentials"
          hint="Bot tokens and webhook URLs travel in plain text. Keep the file somewhere you would keep a password."
        />
        <div className="flex flex-wrap gap-2">
          <button
            onClick={() => downloadBackup.mutate()}
            disabled={downloadBackup.isPending}
            className="btn-primary text-xs"
          >
            <Icon name="download" className="h-3.5 w-3.5" />
            {downloadBackup.isPending
              ? 'Packing…'
              : includeImages
                ? 'Download everything'
                : 'Download database'}
          </button>
          <button
            onClick={() => exportConfig.mutate()}
            disabled={exportConfig.isPending}
            className="btn-quiet text-xs"
          >
            <Icon name="settings" className="h-3.5 w-3.5" />
            {exportConfig.isPending ? 'Packing…' : 'Settings only'}
          </button>
        </div>
        <p className="text-2xs text-faint">
          Settings on their own are for setting up a second instance: cost profiles, notification
          channels and crawl tuning, without the catalogue.
        </p>
      </div>

      <div className="mt-5 space-y-3 border-t border-line pt-3">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-faint">
          Put a copy back
        </h4>
        <p className="text-xs text-muted">
          Restoring replaces this instance's data. The database being replaced is kept on the
          server under a <span className="font-mono">pre-restore</span> name rather than deleted,
          so a mistake here is recoverable.
        </p>
        <div className="flex flex-wrap gap-2">
          <input
            ref={backupInput}
            type="file"
            accept=".zip,application/zip"
            className="hidden"
            onChange={(e) => {
              void pickBackup(e.target.files?.[0])
              e.target.value = ''
            }}
          />
          <button
            onClick={() => backupInput.current?.click()}
            disabled={inspect.isPending}
            className="btn-quiet text-xs"
          >
            <Icon name="upload" className="h-3.5 w-3.5" />
            {inspect.isPending ? 'Reading…' : 'Restore from a backup'}
          </button>

          <input
            ref={configInput}
            type="file"
            accept=".json,application/json"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0]
              if (file) importConfig.mutate(file)
              e.target.value = ''
            }}
          />
          <button
            onClick={() => configInput.current?.click()}
            disabled={importConfig.isPending}
            className="btn-quiet text-xs"
          >
            <Icon name="upload" className="h-3.5 w-3.5" />
            {importConfig.isPending ? 'Applying…' : 'Apply a settings file'}
          </button>
        </div>
        <p className="text-2xs text-faint">
          A settings file only adds and updates. It never deletes anything, and it reports
          whatever it could not match.
        </p>
      </div>

      <Modal
        open={pending !== null}
        onClose={() => setPending(null)}
        title="Restore this backup?"
      >
        {manifest && (
          <div className="space-y-4">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
              <dt className="text-faint">Taken</dt>
              <dd className="tabular-nums">
                {manifest.created_at
                  ? new Date(manifest.created_at).toLocaleString('en-GB')
                  : 'unknown'}
              </dd>
              <dt className="text-faint">Database</dt>
              <dd className="tabular-nums">{bytes(manifest.database_bytes)}</dd>
              <dt className="text-faint">Photos</dt>
              <dd className="tabular-nums">
                {manifest.images
                  ? `${manifest.images.toLocaleString('en-GB')} (${bytes(manifest.image_bytes)})`
                  : 'none in this file'}
              </dd>
              <dt className="text-faint">Credentials</dt>
              <dd>{manifest.includes_secrets ? 'included' : 'not included'}</dd>
            </dl>

            <p className="rounded-card border border-warning/40 bg-warning/10 p-3 text-xs">
              This replaces everything currently in this instance: every watch, every alert and
              the whole catalogue. Your existing database will be kept on the server as a
              <span className="font-mono"> pre-restore </span> file.
            </p>

            {manifest.images ? (
              <Toggle
                checked={restoreImages}
                onChange={setRestoreImages}
                label="Also restore the photos"
                hint="They are written alongside whatever is already cached; nothing is removed."
              />
            ) : null}

            <div className="flex justify-end gap-2">
              <button onClick={() => setPending(null)} className="btn-quiet text-xs">
                Cancel
              </button>
              <button
                onClick={() => restore.mutate(pending.file)}
                disabled={restore.isPending}
                className="btn-primary text-xs"
              >
                {restore.isPending ? 'Restoring…' : 'Replace my data'}
              </button>
            </div>
          </div>
        )}
      </Modal>
    </Card>
  )
}
