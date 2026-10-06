import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { useStore, type Tab } from '../store'
import { AssistantPanel } from './AssistantPanel'
import { Drawer } from './Drawer'
import { GraphView } from './GraphView'
import { IssuesView } from './IssuesView'
import { ConnectionPointsTable, ConnectionsTable, EquipmentTable, PointsTable, SpacesTable } from './ModelTables'
import { SourcesPane } from './SourcesPane'
import { ResizeHandle } from './ResizeHandle'
import { ViewTable } from './ViewTable'
import { TokenStatus } from './TokenStatus'

const MIN_MODEL_WIDTH = 360
const HANDLE = 6

/** A pane size the user drags, remembered across sessions. */
function usePaneSize(key: string, fallback: number, min: number, max: () => number) {
  const [size, setSize] = useState(() => {
    const stored = Number(localStorage.getItem(key))
    return Number.isFinite(stored) && stored >= min ? stored : fallback
  })
  const update = (next: (size: number) => number) => setSize((current) => {
    const value = Math.round(Math.max(min, Math.min(Math.max(min, max()), next(current))))
    localStorage.setItem(key, String(value))
    return value
  })
  return [size, (delta: number) => update((current) => current + delta), () => update(() => fallback)] as const
}

/** Import and export, out of the way of the project status in the top bar. */
function FileMenu({ onImport, importDisabled, items }: { onImport: () => void; importDisabled: boolean; items: { label: string; hint: string; href: string }[] }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const outside = (e: PointerEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false) }
    const escape = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape) }
  }, [open])
  return <div className="menu" ref={ref}>
    <button aria-haspopup="menu" aria-expanded={open} className={open ? 'active' : ''} onClick={() => setOpen(!open)}>File ▾</button>
    {open && <div className="menu-list" role="menu">
      <button role="menuitem" disabled={importDisabled} onClick={() => { setOpen(false); onImport() }}>
        Import Turtle model…<span className="menu-hint">Merge a .ttl file into this project</span>
      </button>
      <hr />
      {items.map((item) => <a key={item.label} role="menuitem" href={item.href} download onClick={() => setOpen(false)}>
        {item.label}<span className="menu-hint">{item.hint}</span>
      </a>)}
    </div>}
  </div>
}

// The project's tabs come from its views (views.toml / workbench.toml, filtered per profile);
// these show until the list arrives. Graph and Issues always follow.
const DEFAULT_TABS: [Tab, string][] = [
  ['points', 'Points'], ['equipment', 'Equipment'], ['connections', 'Connections'],
]
const FIXED_TABS: [Tab, string][] = [['graph', 'Graph'], ['issues', 'Issues']]

export function Workspace() {
  const projectId = useStore((s) => s.projectId)!
  const model = useStore((s) => s.model)
  const tab = useStore((s) => s.tab)
  const setTab = useStore((s) => s.setTab)
  const handleEvent = useStore((s) => s.handleEvent)
  const openProject = useStore((s) => s.openProject)
  const undo = useStore((s) => s.undo)
  const viewing = useStore((s) => s.viewing)
  const viewRevision = useStore((s) => s.viewRevision)
  const views = useStore((s) => s.views)
  const redo = useStore((s) => s.redo)
  const importModel = useStore((s) => s.importModel)
  const reconcile = useStore((s) => s.reconcile)
  const notify = useStore((s) => s.notify)
  const sourcesOpen = useStore((s) => s.sourcesOpen)
  const toggleSources = useStore((s) => s.toggleSources)
  const setDrawerTab = useStore((s) => s.setDrawerTab)
  const fileInput = useRef<HTMLInputElement>(null)
  const [drawerHeight, resizeDrawer, resetDrawer] = usePaneSize('workbench.drawerHeight', 240, 120,
    () => window.innerHeight - 220)
  // Each side tray may grow until the model pane would drop below its minimum width.
  const [sourcesWidth, resizeSources, resetSources] = usePaneSize('workbench.sourcesWidth', 380, 260,
    () => window.innerWidth - assistantWidth - MIN_MODEL_WIDTH - 2 * HANDLE)
  const [assistantWidth, resizeAssistant, resetAssistant] = usePaneSize('workbench.assistantWidth', 400, 300,
    () => window.innerWidth - (sourcesOpen ? sourcesWidth + HANDLE : 0) - MIN_MODEL_WIDTH - HANDLE)

  useEffect(() => {
    const es = new EventSource(api.eventsUrl(projectId))
    let disposed = false
    es.onopen = () => {
      if (!disposed && useStore.getState().projectId === projectId) {
        void reconcile().catch((err) => {
          if (!disposed) notify({ kind: 'error', text: `Could not refresh project: ${(err as Error).message}` })
        })
      }
    }
    es.onmessage = (m) => {
      if (disposed || useStore.getState().projectId !== projectId) return
      try { handleEvent(JSON.parse(m.data)) } catch { /* ignore keepalives */ }
    }
    return () => { disposed = true; es.close() }
  }, [projectId, handleEvent, reconcile, notify])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); void (e.shiftKey ? redo() : undo()) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [undo, redo])

  if (!model) return <div className="loading">Loading project…</div>
  const v = model.revision.validation
  const openViolations = model.issues.filter((i) => i.resolution_state === 'open' && i.severity === 'violation').length
  const dismissedCount = model.issues.filter((i) => i.resolution_state === 'dismissed').length
  const problems = model.issues.filter((i) => i.resolution_state === 'open' && i.severity !== 'suggestion').length
  const counts = { points: model.view.points.length, equipment: model.view.equipment.length, spaces: model.view.spaces.length,
                   connections: model.view.connections.length, connection_points: model.view.connection_points.length,
                   graph: model.view.equipment.length + model.view.spaces.length, issues: problems }
  const listed: [Tab, string][] = views.filter((v) => !v.builtin)
    .map((v) => [v.table ? v.table as Tab : `view:${v.id}`, v.label])
  const tabs = [...(views.length ? listed : DEFAULT_TABS), ...FIXED_TABS]
  const shown = tabs.some(([id]) => id === tab) ? tab : tabs[0][0]  // e.g. Spaces, hidden for this profile
  const tabCount = (id: Tab) => (counts as Record<string, number | undefined>)[id]

  return (
    <div className="workspace">
      <header className="topbar">
        <button className="link" onClick={() => void openProject(null)} title="All projects">◂ Projects</button>
        <h1>{model.info.name}</h1>
        <span className={`profile-badge ${model.info.family}`} title={`Target vocabulary: ${model.info.profile_label}`}>
          {model.info.profile_label.split(' (')[0]}</span>
        <button className="rev" onClick={() => setDrawerTab('history')}
          title={`${model.revision.id}, saved ${new Date(model.revision.created_at).toLocaleString()}: ${model.revision.summary}\nClick to see the full history.`}>
          <span className="rev-id">{model.revision.id}</span>
          <span className="rev-summary">{model.revision.summary}</span>
        </button>
        {v && <button className={`health ${openViolations ? 'bad' : 'good'}`}
          title={`Validated in ${v.duration_s}s: ${openViolations} open violation(s)${dismissedCount ? `, ${dismissedCount} dismissed issue(s)` : ''}. Click to open Issues, which also includes warnings and source findings.`}
          onClick={() => setTab('issues')}>
          {openViolations ? `${openViolations} violation(s)` : dismissedCount ? 'no open violations' : 'model checks pass'}
          {dismissedCount > 0 && <span className="muted"> · {dismissedCount} dismissed</span>}
        </button>}
        <span className="spacer" />
        <TokenStatus key={projectId} projectId={projectId} />
        <button onClick={() => void undo()} disabled={!!viewing || !model.info.can_undo} title="Undo (Ctrl+Z)">Undo</button>
        <button onClick={() => void redo()} disabled={!!viewing || !model.info.can_redo} title="Redo (Ctrl+Shift+Z)">Redo</button>
        <FileMenu importDisabled={!!viewing} onImport={() => fileInput.current?.click()} items={[
          { label: 'Export Turtle (.ttl)', hint: `The model at ${model.revision.id}`, href: api.exportUrl(projectId, model.revision.id, 'ttl') },
          { label: 'Export point table (.csv)', hint: `Points at ${model.revision.id}, with measurement, unit, equipment and sensor type`,
            href: api.exportUrl(projectId, model.revision.id, 'csv') },
        ]} />
        <input ref={fileInput} type="file" accept=".ttl,.turtle" hidden onChange={async (e) => {
          const f = e.target.files?.[0]
          e.target.value = ''
          if (!f || useStore.getState().viewing || useStore.getState().projectId !== projectId) return
          await importModel(f)
        }} />
      </header>
      {viewing && <div className="viewing-banner">
        <span><strong>Viewing {model.revision.id}</strong> from {new Date(model.revision.created_at).toLocaleString()}
          {' '}· {model.revision.summary} — read-only</span>
        <span className="spacer" />
        <a href={api.exportUrl(projectId, model.revision.id, 'ttl')} download>Download .ttl</a>
        <button className="primary" onClick={() => void viewRevision(null)}>Back to current ({model.head})</button>
      </div>}
      <main className="panes" style={{ gridTemplateColumns: [
        ...(sourcesOpen ? [`${sourcesWidth}px`, `${HANDLE}px`] : []), 'minmax(0, 1fr)', `${HANDLE}px`, `${assistantWidth}px`,
      ].join(' ') }}>
        {sourcesOpen && <>
          <SourcesPane />
          <ResizeHandle direction="columns" label="Resize sources tray" onResize={resizeSources} onReset={resetSources} />
        </>}
        <section className="model-pane" style={{ gridTemplateRows: `auto minmax(100px, 1fr) 10px ${drawerHeight}px` }}>
          <nav className="tabs">
            <button className={`sources-toggle ${sourcesOpen ? 'open' : ''}`} onClick={toggleSources}
              aria-pressed={sourcesOpen} title={sourcesOpen ? 'Hide the sources tray' : 'Show the sources tray'}>
              {sourcesOpen ? '◂' : '▸'} Sources
            </button>
            {tabs.map(([id, label]) => (
              <button key={id} className={shown === id ? 'active' : ''} onClick={() => setTab(id)}
                title={id === 'issues' ? 'Validation findings plus source extraction and association issues' : undefined}>
                {label} <span className={`count ${id === 'issues' && tabCount(id) ? 'warn' : ''}`}>{tabCount(id)}</span>
              </button>
            ))}
          </nav>
          <div className="tab-body">
            {shown === 'points' && <PointsTable />}
            {shown === 'equipment' && <EquipmentTable />}
            {shown === 'spaces' && <SpacesTable />}
            {shown === 'connections' && <ConnectionsTable />}
            {shown === 'connection_points' && <ConnectionPointsTable />}
            {shown === 'graph' && <GraphView />}
            {shown === 'issues' && <IssuesView />}
            {shown.startsWith('view:') && <ViewTable key={shown} vid={shown.slice(5)} />}
          </div>
          <ResizeHandle label="Resize inspector tray" onResize={resizeDrawer} onReset={resetDrawer} />
          <Drawer />
        </section>
        <ResizeHandle direction="columns" label="Resize assistant tray" onResize={(delta) => resizeAssistant(-delta)}
          onReset={resetAssistant} />
        <AssistantPanel />
      </main>
    </div>
  )
}
