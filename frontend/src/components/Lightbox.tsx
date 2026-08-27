import { useCallback, useEffect, useState } from 'react'
import { Icon } from '@/components/Icon'

/**
 * The product photos, full size.
 *
 * AmiAmi's gallery is the only good look at a figure before buying, and the
 * thumbnail on the page is far too small to judge a paint job or the state of
 * a used box. This is deliberately plain: the image, arrows, a counter, and
 * every way out a person might reach for - Escape, the backdrop, the close
 * button - because a viewer you cannot dismiss is worse than no viewer.
 */
export function Lightbox({
  images,
  index,
  onClose,
  onIndexChange,
  alt,
}: {
  images: string[]
  index: number
  onClose: () => void
  onIndexChange: (index: number) => void
  alt: string
}) {
  const [loaded, setLoaded] = useState(false)
  const count = images.length

  const step = useCallback(
    (delta: number) => {
      if (count < 2) return
      setLoaded(false)
      onIndexChange((((index + delta) % count) + count) % count)
    },
    [count, index, onIndexChange],
  )

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
      if (event.key === 'ArrowRight') step(1)
      if (event.key === 'ArrowLeft') step(-1)
    }
    window.addEventListener('keydown', onKey)
    // The page behind must not scroll while this is up, or dismissing it
    // leaves you somewhere you did not choose to be.
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = previous
    }
  }, [onClose, step])

  if (!images.length) return null

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={alt}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <button
        onClick={onClose}
        aria-label="Close"
        className="absolute right-4 top-4 rounded-full bg-white/10 p-2 text-white transition-colors hover:bg-white/20"
      >
        <Icon name="close" className="h-5 w-5" />
      </button>

      {count > 1 && (
        <>
          <button
            aria-label="Previous image"
            onClick={(event) => {
              event.stopPropagation()
              step(-1)
            }}
            className="absolute left-2 rounded-full bg-white/10 p-3 text-white transition-colors hover:bg-white/20 sm:left-6"
          >
            <Icon name="chevronLeft" className="h-5 w-5" />
          </button>
          <button
            aria-label="Next image"
            onClick={(event) => {
              event.stopPropagation()
              step(1)
            }}
            className="absolute right-2 rounded-full bg-white/10 p-3 text-white transition-colors hover:bg-white/20 sm:right-6"
          >
            <Icon name="chevronRight" className="h-5 w-5" />
          </button>
        </>
      )}

      <img
        key={images[index]}
        src={images[index]}
        alt={alt}
        onLoad={() => setLoaded(true)}
        onClick={(event) => event.stopPropagation()}
        className={`max-h-[86vh] max-w-full rounded-card object-contain transition-opacity duration-200 ${
          loaded ? 'opacity-100' : 'opacity-0'
        }`}
      />

      {count > 1 && (
        <div
          className="absolute bottom-4 flex max-w-full gap-2 overflow-x-auto px-4"
          onClick={(event) => event.stopPropagation()}
        >
          {images.map((src, position) => (
            <button
              key={src}
              onClick={() => {
                setLoaded(false)
                onIndexChange(position)
              }}
              aria-label={`Image ${position + 1} of ${count}`}
              className={`h-14 w-14 shrink-0 overflow-hidden rounded-control border-2 transition-opacity ${
                position === index
                  ? 'border-accent opacity-100'
                  : 'border-transparent opacity-60 hover:opacity-100'
              }`}
            >
              <img src={src} alt="" loading="lazy" className="h-full w-full object-cover" />
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
