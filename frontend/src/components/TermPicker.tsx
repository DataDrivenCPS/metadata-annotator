import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { useStore } from '../store'
import type { Term } from '../types'

interface Props {
  kind: string
  quantityKind?: string | null
  initial?: string
  allowClear?: boolean
  onPick: (iri: string | null) => void
  onCancel: () => void
}

/** Search the vocabulary by label. Units narrow to the point's quantity kind when known. */
export function TermPicker({ kind, quantityKind, initial = '', allowClear = true, onPick, onCancel }: Props) {
  const [q, setQ] = useState(initial)
  const [options, setOptions] = useState<Term[]>([])
  const [active, setActive] = useState(0)
  const [preloaded, setPreloaded] = useState<Term[] | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const profile = useStore((s) => s.model?.info.profile ?? 'watr')

  useEffect(() => { input.current?.select() }, [])
  useEffect(() => {
    if (kind === 'unit' && quantityKind) api.options('unit', quantityKind, profile).then(setPreloaded).catch(() => setPreloaded(null))
    else if (['process', 'sensor', 'connection', 'medium', 'equipment', 'location'].includes(kind))
      api.options(kind, null, profile).then(setPreloaded)
  }, [kind, quantityKind, profile])

  const local = useMemo(() => {
    if (!preloaded) return null
    const words = q.toLowerCase().split(/\s+/).filter(Boolean)
    return preloaded.filter((t) => words.every((w) => `${t.label} ${t.symbol ?? ''} ${t.iri}`.toLowerCase().includes(w))).slice(0, 40)
  }, [preloaded, q])

  useEffect(() => {
    if (local) { setOptions(local); setActive(0); return }
    if (q.trim().length < 2) { setOptions([]); return }
    const h = setTimeout(() => {
      api.searchTerms(q, kind, profile).then((r) => { setOptions(r); setActive(0) }).catch(() => setOptions([]))
    }, 150)
    return () => clearTimeout(h)
  }, [q, kind, local, profile])

  return (
    <div className="term-picker" onClick={(e) => e.stopPropagation()}>
      <input
        ref={input} autoFocus value={q} placeholder={`Search ${kind === 'point_class' ? 'point types' : kind.replace('_', ' ')}…`}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onCancel()
          else if (e.key === 'ArrowDown') { e.preventDefault(); setActive((a) => Math.min(a + 1, options.length - 1)) }
          else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)) }
          else if (e.key === 'Enter' && options[active]) onPick(options[active].iri)
        }}
        onBlur={() => setTimeout(onCancel, 150)}
      />
      <ul className="term-options">
        {allowClear && <li className="clear" onMouseDown={() => onPick(null)}>— none —</li>}
        {options.map((t, i) => (
          <li key={t.iri} className={i === active ? 'active' : ''} onMouseDown={() => onPick(t.iri)} title={t.iri}>
            <span>{t.label}</span>
            {t.symbol && <span className="sym">{t.symbol}</span>}
            <span className="iri">{shortIri(t.iri)}</span>
          </li>
        ))}
        {options.length === 0 && q.length >= 2 && <li className="empty">No matching terms</li>}
      </ul>
    </div>
  )
}

export function shortIri(iri: string) {
  const m = iri.match(/[#/]([^#/]+)$/)
  const ns = iri.includes('watermetadata') ? 'watr' : iri.includes('standard223') ? 's223'
    : iri.includes('brickschema.org') ? 'brick'
    : iri.includes('qudt.org/vocab/unit') ? 'unit' : iri.includes('quantitykind') ? 'qk' : ''
  return ns ? `${ns}:${m?.[1] ?? iri}` : (m?.[1] ?? iri)
}
