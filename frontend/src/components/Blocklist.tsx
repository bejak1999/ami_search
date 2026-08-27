import { useState } from 'react'

/**
 * Things you never want to see again.
 *
 * Deliberately scoped to browsing - search results and the discovery feed -
 * and never to a watch. A watch is something asked for by name, so quietly
 * hiding its results would turn a filter into a silent failure.
 */
export function Blocklist({
  values,
  onChange,
  placeholder,
  empty,
}: {
  values: string[]
  onChange: (next: string[]) => void
  placeholder: string
  empty: string
}) {
  const [draft, setDraft] = useState('')

  function add() {
    const cleaned = draft.trim().toLowerCase()
    if (!cleaned) return
    // Case-insensitive, because "Nendoroid" and "nendoroid" are the same
    // request and two entries that look identical are just confusing.
    if (!values.some((value) => value.toLowerCase() === cleaned)) {
      onChange([...values, cleaned])
    }
    setDraft('')
  }

  return (
    <div className="space-y-2">
      <div className="flex gap-2">
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              add()
            }
          }}
          placeholder={placeholder}
          className="field flex-1"
        />
        <button type="button" onClick={add} disabled={!draft.trim()} className="btn-quiet text-sm">
          Add
        </button>
      </div>

      {values.length ? (
        <div className="flex flex-wrap gap-1.5">
          {values.map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => onChange(values.filter((entry) => entry !== value))}
              title="Remove"
              className="chip gap-1 hover:border-danger hover:text-danger"
            >
              {value}
              <span aria-hidden>&times;</span>
            </button>
          ))}
        </div>
      ) : (
        <p className="text-xs text-faint">{empty}</p>
      )}
    </div>
  )
}
