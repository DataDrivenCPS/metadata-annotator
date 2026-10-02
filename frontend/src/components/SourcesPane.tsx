import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { useStore } from '../store'
import type { CsvGrid, CsvImportConfig, CsvPreview, Observation, Source } from '../types'
import { MAX_BUILD_PAGES, parsePageSelection } from '../documents'

const LAYOUTS: [CsvImportConfig['layout'], string, string][] = [
  ['row_points', 'Rows describe points', 'One point per row: a name column plus optional metadata columns.'],
  ['header_points', 'Headers are point names', 'A data export: each column header is a point name.'],
  ['column_points', 'Columns describe points', 'A vendor sheet: one row of names, other rows describe them.'],
]

export function SourcesPane() {
  const projectId = useStore((s) => s.projectId)!
  const notify = useStore((s) => s.notify)
  const sourcesVersion = useStore((s) => s.sourcesVersion)
  const [sources, setSources] = useState<Source[]>([])
  const [active, setActive] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const input = useRef<HTMLInputElement>(null)

  const load = useCallback(() => api.sources(projectId).then(setSources), [projectId])
  useEffect(() => { void load() }, [load, sourcesVersion])

  const upload = async (files: FileList | null) => {
    if (!files?.length) return
    setUploading(true)
    try {
      let last: Source | null = null
      for (const f of Array.from(files)) last = await api.uploadSource(projectId, f)
      await load()
      if (last) setActive(last.id)
    } catch (e) {
      notify({ kind: 'error', text: `Upload failed: ${(e as Error).message}` })
    } finally {
      setUploading(false)
    }
  }

  const current = sources.find((s) => s.id === active) ?? null
  return (
    <aside className="sources-pane"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => { e.preventDefault(); void upload(e.dataTransfer.files) }}>
      <div className="pane-head">
        <h2>Sources</h2>
        <span className="spacer" />
        <button className="primary" disabled={uploading} onClick={() => input.current?.click()}>
          {uploading ? 'Uploading…' : 'Upload…'}
        </button>
        <input ref={input} type="file" multiple hidden accept=".csv,.tsv,.txt,.md,.json,.yaml,.yml,.log,.docx,.pdf,.png,.jpg,.jpeg,.webp"
          onChange={(e) => { void upload(e.target.files); e.target.value = '' }} />
      </div>
      {sources.length === 0 ? (
        <div className="drop-hint">
          Upload CSV/TSV, images, PDFs, Word (.docx), or text documents, or drop files here.
          <br /><span className="muted">To start from an existing Turtle model, use “Import model…” above.</span>
        </div>
      ) : (
        <ul className="source-list">
          {sources.map((s) => (
            <li key={s.id} className={s.id === active ? 'active' : ''} onClick={() => setActive(s.id === active ? null : s.id)}>
              <span className={`src-kind ${s.kind}`}>{s.kind === 'csv' ? 'CSV' : s.kind === 'image' ? 'IMG' : s.kind === 'pdf' ? 'PDF' : 'DOC'}</span>
              <span className="src-name" title={s.filename}>{s.filename}</span>
              <span className="muted">{s.kind === 'csv'
                ? (s.status === 'configured'
                  ? (s.modeled_count ? `${s.modeled_count}/${s.observation_count} in model` : `${s.observation_count} records`)
                  : 'needs mapping')
                : s.kind === 'image' ? `${s.width}×${s.height}` : s.kind === 'pdf' ? `${s.page_count} pages` : 'text'}</span>
            </li>
          ))}
        </ul>
      )}
      {current && (
        <div className="source-view">
          {current.kind === 'csv' ? <CsvSource source={current} onConfirmed={load} />
            : current.kind === 'image' ? <ImageView key={current.id} source={current} />
            : <DocumentView key={current.id} source={current} />}
        </div>
      )}
    </aside>
  )
}

// --------------------------------------------------------------------- CSV

function CsvSource({ source, onConfirmed }: { source: Source; onConfirmed: () => void }) {
  const [tab, setTab] = useState<'mapping' | 'records'>(source.status === 'configured' ? 'records' : 'mapping')
  useEffect(() => { setTab(source.status === 'configured' ? 'records' : 'mapping') }, [source.id, source.status])
  return (
    <div className="csv-source">
      <nav className="tabs small">
        <button className={tab === 'mapping' ? 'active' : ''} onClick={() => setTab('mapping')}>Structure</button>
        <button className={tab === 'records' ? 'active' : ''} disabled={source.status !== 'configured'}
          onClick={() => setTab('records')}>Records {source.observation_count ? <span className="count">{source.observation_count}</span> : null}</button>
      </nav>
      {tab === 'mapping' ? <CsvView source={source} onConfirmed={() => { onConfirmed(); setTab('records') }} /> : <RecordsView source={source} />}
    </div>
  )
}

function RecordsView({ source }: { source: Source }) {
  const projectId = useStore((s) => s.projectId)!
  const sourcesVersion = useStore((s) => s.sourcesVersion)
  const startBuild = useStore((s) => s.startBuild)
  const runs = useStore((s) => s.runs)
  const activeRunId = useStore((s) => s.activeRunId)
  const click = useStore((s) => s.click)
  const setTab = useStore((s) => s.setTab)
  const rows = useStore((s) => s.rows)
  const family = useStore((s) => s.model?.info.family)
  const [obs, setObs] = useState<Observation[] | null>(null)
  const [filter, setFilter] = useState('')
  const [building, setBuilding] = useState(false)
  const [hint, setHint] = useState('')
  useEffect(() => {
    api.observations(projectId, source.id).then((o) => setObs(o.filter((x) => x.status !== 'superseded')))
  }, [projectId, source.id, sourcesVersion])
  if (!obs) return <p className="muted">Loading records…</p>
  const q = filter.trim().toLowerCase()
  const shown = q ? obs.filter((o) => JSON.stringify(o.content).toLowerCase().includes(q)) : obs
  const keys = Array.from(new Set(obs.flatMap((o) => Object.keys(o.content.metadata ?? {}))))
  const inModel = obs.filter((o) => o.entity_id).length
  const run = activeRunId ? runs[activeRunId] : null
  const running = run && run.kind === 'build' && (run.status === 'queued' || run.status === 'running')
  return (
    <div className="records-view">
      <div className="build-box">
        <div className="build-head">
          <strong>{inModel} of {obs.length} in the model</strong>
          {inModel < obs.length && !building && (
            <button className="primary" disabled={!!running} onClick={() => setBuilding(true)}>
              {inModel ? 'Build the rest…' : 'Build model from these records…'}</button>
          )}
        </div>
        {building && (
          <div className="build-form">
            <p className="muted small">
              The assistant follows the BuildingMOTIF skill's point-list workflow: it works out the naming
              convention, maps each distinct point {family === 'brick' ? 'suffix to a Brick point class' : 'description to a point kind, unit and sensor'},
              classifies equipment by the points it carries, and proposes the whole model for your review.
              Anything it can't map with a verified term is left for you to resolve.
            </p>
            <textarea rows={2} placeholder="Anything it should know? e.g. “A1, A2, E1 are AHUs; RMxxxx entries are VAV boxes with reheat.”"
              value={hint} onChange={(e) => setHint(e.target.value)} />
            <div className="csv-actions">
              <button className="primary" onClick={async () => { if (await startBuild([source.id], hint.trim())) setBuilding(false) }}>
                Build</button>
              <button onClick={() => setBuilding(false)}>Cancel</button>
            </div>
          </div>
        )}
        {running && <p className="muted small"><span className="spinner" /> Building — progress is in the assistant panel.</p>}
      </div>
      <div className="records-head">
        <input className="filter" placeholder="Filter records…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <span className="muted small">{shown.length} of {obs.length}</span>
      </div>
      <div className="grid-scroll tall">
        <table className="raw-grid">
          <thead><tr><th>Where</th><th>Point name</th>{keys.map((k) => <th key={k}>{k}</th>)}<th>Model</th></tr></thead>
          <tbody>{shown.slice(0, 1000).map((o) => (
            <tr key={o.id} title={o.id}>
              <th>{o.location.kind === 'csv_row' ? `row ${(o.location.row ?? 0) + 1}` : `col ${(o.location.column ?? 0) + 1}`}</th>
              <td className="name">{o.content.name}</td>
              {keys.map((k) => <td key={k}>{o.content.metadata?.[k] ?? ''}</td>)}
              <td>{o.entity_id && rows.get(o.entity_id)
                ? <button className="link" onClick={() => { const r = rows.get(o.entity_id!)!
                    setTab(r.kind === 'point' ? 'points' : r.kind === 'connection' ? 'connections' : 'equipment')
                    click({ id: o.entity_id!, relationship: r.kind === 'connection' }, { ctrl: false, shift: false }, []) }}>
                    in model</button>
                : <span className="obs-status">not modeled</span>}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      <p className="muted small">Source records stay separate from the model; each modeled point cites its record as evidence.</p>
    </div>
  )
}

function CsvView({ source, onConfirmed }: { source: Source; onConfirmed: () => void }) {
  const projectId = useStore((s) => s.projectId)!
  const notify = useStore((s) => s.notify)
  const [grid, setGrid] = useState<CsvGrid | null>(null)
  const [cfg, setCfg] = useState<CsvImportConfig | null>(null)
  const [preview, setPreview] = useState<CsvPreview | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    setGrid(null); setPreview(null); setError(null)
    api.sourceGrid(projectId, source.id).then((g) => { setGrid(g); setCfg(g.config) }).catch((e) => setError(e.message))
  }, [projectId, source.id])

  useEffect(() => {
    if (!cfg) return
    const t = setTimeout(() => {
      api.sourcePreview(projectId, source.id, cfg).then((p) => { setPreview(p); setError(null) }).catch((e) => setError(e.message))
    }, 200)
    return () => clearTimeout(t)
  }, [projectId, source.id, cfg])

  if (error && !grid) return <p className="error-text">{error}</p>
  if (!grid || !cfg) return <p className="muted">Reading file…</p>
  const cols = Array.from({ length: grid.columns }, (_, i) => i)
  const set = (patch: Partial<CsvImportConfig>) => setCfg({ ...cfg, ...patch })
  const toggle = (list: number[], i: number) => (list.includes(i) ? list.filter((x) => x !== i) : [...list, i].sort((a, b) => a - b))
  const colLabel = (i: number) => {
    const h = cfg.header_row !== null && cfg.layout !== 'column_points' ? grid.rows[cfg.header_row]?.[i] : ''
    return `${i + 1}${h ? ` · ${h}` : ''}`
  }

  const cellClass = (r: number, c: number) => {
    if (cfg.layout === 'row_points') {
      if (cfg.header_row === r) return 'hdr'
      if (c === cfg.name_column) return 'name'
      if (cfg.metadata_columns.includes(c)) return 'meta'
    } else if (cfg.layout === 'header_points') {
      if (r === cfg.header_row) return cfg.excluded_columns.includes(c) ? 'excluded' : 'name'
      if (cfg.excluded_columns.includes(c)) return 'excluded'
    } else {
      if (r === cfg.name_row && c >= cfg.first_data_column) return 'name'
      if (cfg.metadata_rows.includes(r) && c >= cfg.first_data_column) return 'meta'
      if (c < cfg.first_data_column) return 'hdr'
    }
    return ''
  }

  const confirm = async () => {
    setBusy(true)
    try {
      const r = await api.sourceConfirm(projectId, source.id, cfg)
      notify({ kind: 'success', text: `Captured ${r.observations} point records from ${source.filename}`
        + (r.superseded ? ` (replacing ${r.superseded} from the previous mapping)` : '') })
      onConfirmed()
      setGrid({ ...grid, confirmed: true })
    } catch (e) { notify({ kind: 'error', text: (e as Error).message }) } finally { setBusy(false) }
  }

  return (
    <div className="csv-view">
      <p className="muted small">Suggested: {grid.reason}</p>
      <label className="field">Structure
        <select value={cfg.layout} onChange={(e) => {
          const layout = e.target.value as CsvImportConfig['layout']
          set(layout === grid.suggested.layout ? grid.suggested : {
            layout, header_row: layout === 'column_points' ? null : 0, name_column: 0, name_row: 0,
            metadata_columns: [], excluded_columns: [], metadata_rows: [], first_data_row: null, first_data_column: 1 })
        }}>
          {LAYOUTS.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </select>
      </label>
      <p className="muted small">{LAYOUTS.find((l) => l[0] === cfg.layout)![2]}</p>

      {cfg.layout === 'row_points' && <>
        <label className="check"><input type="checkbox" checked={cfg.header_row !== null}
          onChange={(e) => set({ header_row: e.target.checked ? 0 : null, first_data_row: null })} /> First row is a header</label>
        <label className="field">Point name column
          <select value={cfg.name_column ?? 0} onChange={(e) => set({ name_column: Number(e.target.value),
            metadata_columns: cfg.metadata_columns.filter((c) => c !== Number(e.target.value)) })}>
            {cols.map((c) => <option key={c} value={c}>{colLabel(c)}</option>)}
          </select>
        </label>
        {cols.length > 1 && <div className="field">Metadata columns
          <div className="checks">{cols.filter((c) => c !== cfg.name_column).map((c) => (
            <label key={c} className="check"><input type="checkbox" checked={cfg.metadata_columns.includes(c)}
              onChange={() => set({ metadata_columns: toggle(cfg.metadata_columns, c) })} />{colLabel(c)}</label>
          ))}</div>
        </div>}
      </>}
      {cfg.layout === 'header_points' && <>
        <label className="field">Header row (point names)
          <input type="number" min={1} max={grid.total_rows} value={(cfg.header_row ?? 0) + 1}
            onChange={(e) => set({ header_row: Math.max(0, Number(e.target.value) - 1) })} />
        </label>
        <div className="field">Columns that are not points
          <div className="checks">{cols.map((c) => (
            <label key={c} className="check"><input type="checkbox" checked={cfg.excluded_columns.includes(c)}
              onChange={() => set({ excluded_columns: toggle(cfg.excluded_columns, c) })} />{colLabel(c)}</label>
          ))}</div>
        </div>
      </>}
      {cfg.layout === 'column_points' && <>
        <label className="field">Row with point names
          <input type="number" min={1} max={grid.total_rows} value={(cfg.name_row ?? 0) + 1}
            onChange={(e) => set({ name_row: Math.max(0, Number(e.target.value) - 1),
              metadata_rows: cfg.metadata_rows.filter((r) => r !== Number(e.target.value) - 1) })} />
        </label>
        <label className="field">First point column
          <input type="number" min={1} max={grid.columns} value={cfg.first_data_column + 1}
            onChange={(e) => set({ first_data_column: Math.max(0, Number(e.target.value) - 1) })} />
        </label>
        <div className="field">Metadata rows
          <div className="checks">{grid.rows.slice(0, 20).map((row, r) => r !== cfg.name_row && (
            <label key={r} className="check"><input type="checkbox" checked={cfg.metadata_rows.includes(r)}
              onChange={() => set({ metadata_rows: toggle(cfg.metadata_rows, r) })} />{r + 1} · {row[0]}</label>
          ))}</div>
        </div>
      </>}

      <h5>File ({grid.total_rows} rows{grid.total_rows > grid.rows.length ? `, first ${grid.rows.length} shown` : ''})</h5>
      <div className="grid-scroll">
        <table className="raw-grid">
          <thead><tr><th />{cols.map((c) => <th key={c}>{c + 1}</th>)}</tr></thead>
          <tbody>{grid.rows.map((row, r) => (
            <tr key={r}><th>{r + 1}</th>{cols.map((c) => <td key={c} className={cellClass(r, c)}>{row[c] ?? ''}</td>)}</tr>
          ))}</tbody>
        </table>
      </div>

      <h5>Result {preview && `— ${preview.total} point records`}</h5>
      {error && <p className="error-text">{error}</p>}
      {preview && <>
        {Object.keys(preview.duplicates).length > 0 && (
          <p className="warn-text small">Repeated names are kept as separate records: {Object.entries(preview.duplicates)
            .slice(0, 5).map(([n, k]) => `${n} ×${k}`).join(', ')}</p>
        )}
        <div className="grid-scroll short">
          <table className="raw-grid">
            <thead><tr><th>Where</th><th>Point name</th>{preview.metadata_keys.map((k) => <th key={k}>{k}</th>)}</tr></thead>
            <tbody>{preview.records.slice(0, 50).map((rec, i) => (
              <tr key={i}>
                <th>{rec.location.row !== undefined && rec.location.kind === 'csv_row' ? `row ${rec.location.row + 1}` : `col ${(rec.location.column ?? 0) + 1}`}</th>
                <td className="name">{rec.name}</td>
                {preview.metadata_keys.map((k) => <td key={k}>{rec.metadata[k] ?? ''}</td>)}
              </tr>
            ))}</tbody>
          </table>
        </div>
      </>}
      <div className="csv-actions">
        <button className="primary" disabled={busy || !preview || preview.total === 0} onClick={() => void confirm()}>
          {grid.confirmed ? 'Re-confirm mapping' : 'Confirm mapping'}
        </button>
        {grid.confirmed && <span className="muted small">Re-confirming replaces records that aren't linked to the model yet.</span>}
      </div>
    </div>
  )
}

// -------------------------------------------------------------------- image

type Box = [number, number, number, number]

function ImageView({ source }: { source: Source }) {
  const projectId = useStore((s) => s.projectId)!
  const selection = useStore((s) => s.selection)
  const setSelection = useStore((s) => s.setSelection)
  const frame = useRef<HTMLDivElement>(null)
  const [view, setView] = useState({ scale: 1, x: 0, y: 0 })
  const [mode, setMode] = useState<'pan' | 'select'>('pan')
  const [drag, setDrag] = useState<{ sx: number; sy: number; ox: number; oy: number } | null>(null)
  const [draft, setDraft] = useState<Box | null>(null)
  const W = source.width ?? 1
  const H = source.height ?? 1

  const fit = useCallback(() => {
    const el = frame.current
    if (!el) return
    const s = Math.min(el.clientWidth / W, el.clientHeight / H)
    setView({ scale: s, x: (el.clientWidth - W * s) / 2, y: (el.clientHeight - H * s) / 2 })
  }, [W, H])
  useEffect(() => { fit() }, [fit, source.id])

  const region = selection.source_regions.find((r) => r.source_id === source.id && r.bbox)?.bbox as Box | undefined
  const toImage = (e: React.MouseEvent) => {
    const rect = frame.current!.getBoundingClientRect()
    return [(e.clientX - rect.left - view.x) / view.scale, (e.clientY - rect.top - view.y) / view.scale] as const
  }
  const clamp = (v: number, max: number) => Math.max(0, Math.min(max, v))

  return (
    <div className="image-view">
      <DocumentBuild source={source} />
      <div className="image-tools">
        <button className={mode === 'pan' ? 'active' : ''} onClick={() => setMode('pan')}>Pan</button>
        <button className={mode === 'select' ? 'active' : ''} onClick={() => setMode('select')}>Select region</button>
        <button onClick={() => setView((v) => ({ ...v, scale: v.scale * 1.25 }))}>+</button>
        <button onClick={() => setView((v) => ({ ...v, scale: v.scale / 1.25 }))}>−</button>
        <button onClick={fit}>Fit</button>
        {region && <button className="link" onClick={() => setSelection({ ...selection,
          source_regions: selection.source_regions.filter((r) => r.source_id !== source.id) })}>clear region</button>}
      </div>
      <div ref={frame} className={`image-frame ${mode}`}
        onWheel={(e) => {
          const rect = frame.current!.getBoundingClientRect()
          const mx = e.clientX - rect.left
          const my = e.clientY - rect.top
          const k = e.deltaY < 0 ? 1.15 : 1 / 1.15
          setView((v) => ({ scale: v.scale * k, x: mx - (mx - v.x) * k, y: my - (my - v.y) * k }))
        }}
        onMouseDown={(e) => {
          if (mode === 'pan') setDrag({ sx: e.clientX, sy: e.clientY, ox: view.x, oy: view.y })
          else { const [x, y] = toImage(e); setDraft([clamp(x, W), clamp(y, H), 0, 0]) }
        }}
        onMouseMove={(e) => {
          if (drag) setView((v) => ({ ...v, x: drag.ox + e.clientX - drag.sx, y: drag.oy + e.clientY - drag.sy }))
          else if (draft) { const [x, y] = toImage(e); setDraft([draft[0], draft[1], clamp(x, W) - draft[0], clamp(y, H) - draft[1]]) }
        }}
        onMouseUp={() => {
          setDrag(null)
          if (draft) {
            const [x, y, w, h] = draft
            const box: Box = [Math.round(Math.min(x, x + w)), Math.round(Math.min(y, y + h)), Math.round(Math.abs(w)), Math.round(Math.abs(h))]
            if (box[2] > 4 && box[3] > 4) setSelection({ ...selection,
              source_regions: [...selection.source_regions.filter((r) => r.source_id !== source.id), { source_id: source.id, bbox: box }] })
            setDraft(null)
          }
        }}
        onMouseLeave={() => { setDrag(null); setDraft(null) }}>
        <div className="image-canvas" style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})`, width: W, height: H }}>
          <img src={api.sourceFileUrl(projectId, source.id)} width={W} height={H} draggable={false} alt={source.filename} />
          {[region, draft].map((b, i) => b && (
            <div key={i} className={`region ${i ? 'draft' : ''}`} style={{
              left: Math.min(b[0], b[0] + b[2]), top: Math.min(b[1], b[1] + b[3]),
              width: Math.abs(b[2]), height: Math.abs(b[3]), borderWidth: 2 / view.scale }} />
          ))}
        </div>
      </div>
      <p className="muted small">
        {region ? `Region ${region[2]}×${region[3]} px selected as evidence for the assistant.` : 'Scroll to zoom. Use “Select region” to mark part of the diagram as evidence.'}
        {' '}Build from the full image using the button above, or ask the assistant about the selected region.
      </p>
    </div>
  )
}

// --------------------------------------------------------- document extraction

function DocumentBuild({ source }: { source: Source }) {
  const startBuild = useStore((s) => s.startBuild)
  const runs = useStore((s) => s.runs)
  const providers = useStore((s) => s.status?.providers)
  const provider = useStore((s) => s.provider)
  const notify = useStore((s) => s.notify)
  const [hint, setHint] = useState('')
  const [pages, setPages] = useState(() => `1${(source.page_count ?? 1) > 1 ? `-${Math.min(MAX_BUILD_PAGES, source.page_count!)}` : ''}`)
  const [starting, setStarting] = useState(false)
  const selectedProvider = providers?.find((p) => provider ? p.name === provider : p.default)
  const vision = selectedProvider?.supports_images ?? false
  const running = Object.values(runs).some((r) => r.status === 'queued' || r.status === 'running')
  const canBuild = source.kind !== 'image' || vision

  const build = async () => {
    try {
      const sourcePages = source.kind === 'pdf' ? { [source.id]: parsePageSelection(pages, source.page_count ?? 0) } : undefined
      setStarting(true)
      await startBuild([source.id], hint.trim(), sourcePages)
    } catch (e) { notify({ kind: 'error', text: (e as Error).message }) }
    finally { setStarting(false) }
  }
  return <div className="build-box document-build">
    <strong>Build model from {source.kind === 'image' ? 'image' : source.kind === 'pdf' ? 'PDF' : 'document'}</strong>
    <p className="muted small">The assistant extracts equipment, points, and supported connections. Review the proposed changes before applying.</p>
    {source.kind === 'pdf' && <label className="document-pages">Pages to read
      <input value={pages} onChange={(e) => setPages(e.target.value)} placeholder="1-3, 5" />
      <span className="muted small">Up to {MAX_BUILD_PAGES} pages per build; you can build more pages afterwards.</span>
    </label>}
    <textarea rows={2} value={hint} onChange={(e) => setHint(e.target.value)}
      placeholder="What should it extract? Include any naming conventions or relevant context." />
    {source.filename.toLowerCase().endsWith('.docx') && <p className="muted small">Reads Word text and tables. Export embedded diagrams as images or PDF to include them.</p>}
    {!vision && source.kind === 'image' && <p className="warn-text small">Choose a model that supports images in the assistant panel.</p>}
    {!vision && source.kind === 'pdf' && <p className="muted small">This model reads PDF text only. Choose a model that supports images to read scans or diagrams.</p>}
    <button className="primary" disabled={!canBuild || starting || running} onClick={() => void build()}>
      {starting ? 'Starting…' : running ? 'Assistant is working…' : source.kind === 'pdf' ? 'Build from selected pages' : 'Build model'}
    </button>
  </div>
}

function DocumentView({ source }: { source: Source }) {
  const projectId = useStore((s) => s.projectId)!
  const [page, setPage] = useState(1)
  const [result, setResult] = useState<{ page: number; preview?: { text: string; truncated: boolean }; error?: string } | null>(null)
  const preview = result?.page === page ? result.preview : null
  const error = result?.page === page ? result.error : null
  useEffect(() => {
    let active = true
    api.documentPreview(projectId, source.id, page).then((preview) => { if (active) setResult({ page, preview }) })
      .catch((e) => { if (active) setResult({ page, error: (e as Error).message }) })
    return () => { active = false }
  }, [projectId, source.id, page])
  return <div className="document-view">
    <DocumentBuild source={source} />
    {source.kind === 'pdf' && <>
      <div className="document-navigation">
        <button disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button>
        <span>Page {page} of {source.page_count}</span>
        <button disabled={page >= (source.page_count ?? 1)} onClick={() => setPage(page + 1)}>Next</button>
        <a className="button" href={api.sourceFileUrl(projectId, source.id)} target="_blank" rel="noreferrer">Open PDF</a>
      </div>
      <div className="document-page"><img src={api.sourcePageUrl(projectId, source.id, page)} alt={`${source.filename}, page ${page}`} /></div>
    </>}
    {error && <p className="error-text">{error}</p>}
    {preview && <details open={source.kind !== 'pdf'}>
      <summary>Readable text{preview.truncated ? ' (preview truncated)' : ''}</summary>
      <pre className="document-text">{preview.text || 'No text layer. A model that supports images can read this page visually.'}</pre>
    </details>}
  </div>
}
