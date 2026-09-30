import { useEffect, useRef } from 'react'
import { api } from '../api'
import { useStore, type Tab } from '../store'
import { AssistantPanel } from './AssistantPanel'
import { Drawer } from './Drawer'
import { GraphView } from './GraphView'
import { ConnectionsTable, EquipmentTable, PointsTable } from './ModelTables'
import { SourcesPane } from './SourcesPane'

const TABS: [Tab, string][] = [['points', 'Points'], ['equipment', 'Equipment'], ['connections', 'Connections'], ['graph', 'Graph']]

export function Workspace() {
  const projectId = useStore((s) => s.projectId)!
  const model = useStore((s) => s.model)
  const tab = useStore((s) => s.tab)
  const setTab = useStore((s) => s.setTab)
  const handleEvent = useStore((s) => s.handleEvent)
  const openProject = useStore((s) => s.openProject)
  const undo = useStore((s) => s.undo)
  const redo = useStore((s) => s.redo)
  const reload = useStore((s) => s.reload)
  const notify = useStore((s) => s.notify)
  const sourcesOpen = useStore((s) => s.sourcesOpen)
  const toggleSources = useStore((s) => s.toggleSources)
  const sourcesWide = useStore((s) => s.sourcesWide)
  const fileInput = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const es = new EventSource(api.eventsUrl(projectId))
    es.onmessage = (m) => { try { handleEvent(JSON.parse(m.data)) } catch { /* ignore keepalives */ } }
    return () => es.close()
  }, [projectId, handleEvent])

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
  const counts = { points: model.view.points.length, equipment: model.view.equipment.length,
                   connections: model.view.connections.length, graph: model.view.equipment.length }

  return (
    <div className="workspace">
      <header className="topbar">
        <button className="link" onClick={() => void openProject(null)} title="All projects">◂ Projects</button>
        <h1>{model.info.name}</h1>
        <span className={`profile-badge ${model.info.family}`} title={`Target vocabulary: ${model.info.profile_label}`}>
          {model.info.profile_label.split(' (')[0]}</span>
        <span className="rev" title={model.revision.summary}>
          {model.head} · saved
        </span>
        {v && <span className={`health ${v.violations ? 'bad' : 'good'}`}
          title={`Validated in ${v.duration_s}s against the loaded vocabulary`}>
          {v.violations ? `${v.violations} problem(s)` : 'model checks pass'}
        </span>}
        <span className="spacer" />
        <button className={sourcesOpen ? 'active' : ''} onClick={toggleSources} title="Show or hide uploaded sources">Sources</button>
        <button onClick={() => void undo()} disabled={!model.info.can_undo} title="Undo (Ctrl+Z)">Undo</button>
        <button onClick={() => void redo()} disabled={!model.info.can_redo} title="Redo (Ctrl+Shift+Z)">Redo</button>
        <button onClick={() => fileInput.current?.click()} title="Merge an existing Turtle model into this project">Import model…</button>
        <input ref={fileInput} type="file" accept=".ttl,.turtle" hidden onChange={async (e) => {
          const f = e.target.files?.[0]
          e.target.value = ''
          if (!f) return
          try { const r = await api.importModel(projectId, f); await reload(); notify({ kind: 'success', text: r.summary }) }
          catch (err) { notify({ kind: 'error', text: `Import failed: ${(err as Error).message}` }) }
        }} />
        <a className="button" href={api.exportUrl(projectId, model.head, 'ttl')} download>Export Turtle</a>
        <a className="button" href={api.exportUrl(projectId, model.head, 'csv')} download>Export point table</a>
      </header>
      <main className={`panes ${sourcesOpen ? 'with-sources' : ''} ${sourcesOpen && sourcesWide ? 'wide' : ''}`}>
        {sourcesOpen && <SourcesPane />}
        <section className="model-pane">
          <nav className="tabs">
            {TABS.map(([id, label]) => (
              <button key={id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}>
                {label} <span className="count">{counts[id]}</span>
              </button>
            ))}
          </nav>
          <div className="tab-body">
            {tab === 'points' && <PointsTable />}
            {tab === 'equipment' && <EquipmentTable />}
            {tab === 'connections' && <ConnectionsTable />}
            {tab === 'graph' && <GraphView />}
          </div>
          <Drawer />
        </section>
        <AssistantPanel />
      </main>
    </div>
  )
}
