import { useEffect, useState } from 'react'
import { api } from './api'
import { Workspace } from './components/Workspace'
import { useStore } from './store'
import type { ProjectInfo } from './types'

export default function App() {
  const status = useStore((s) => s.status)
  const loadStatus = useStore((s) => s.loadStatus)
  const projectId = useStore((s) => s.projectId)
  const openProject = useStore((s) => s.openProject)
  const toast = useStore((s) => s.toast)
  const notify = useStore((s) => s.notify)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        await loadStatus()
        const s = useStore.getState().status
        if (!s?.vocabulary.ready && !s?.vocabulary.error) timer = setTimeout(poll, 1000)
        else if (s?.vocabulary.ready && !useStore.getState().projectId) {
          const last = localStorage.getItem('workbench.project')
          if (last) openProject(last).catch(() => openProject(null))
        }
      } catch (e) {
        setError(`Cannot reach the workbench server: ${(e as Error).message}`)
        timer = setTimeout(poll, 2000)
      }
    }
    void poll()
    return () => clearTimeout(timer)
  }, [loadStatus, openProject])

  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => notify(null), toast.kind === 'error' ? 12000 : 6000)
    return () => clearTimeout(t)
  }, [toast, notify])

  let body
  if (!status) body = <div className="loading">{error ?? 'Connecting…'}</div>
  else if (status.vocabulary.error) body = <div className="loading error-text">Vocabulary failed to load: {status.vocabulary.error}</div>
  else if (!status.vocabulary.ready) body = <div className="loading"><span className="spinner" /> Loading the ontology vocabulary (first start builds a cache and takes about a minute)…</div>
  else if (!projectId) body = <ProjectPicker />
  else body = <Workspace />

  return (
    <>
      {body}
      {toast && (
        <div className={`toast ${toast.kind}`}>
          <span>{toast.text}</span>
          {toast.action && <button onClick={() => { toast.action!.run(); notify(null) }}>{toast.action.label}</button>}
          <button className="link" onClick={() => notify(null)}>×</button>
        </div>
      )}
    </>
  )
}

function ProjectPicker() {
  const openProject = useStore((s) => s.openProject)
  const status = useStore((s) => s.status)!
  const [projects, setProjects] = useState<ProjectInfo[] | null>(null)
  const [name, setName] = useState('')
  const [profile, setProfile] = useState(status.default_vocabulary)
  const [busy, setBusy] = useState(false)
  const chosen = status.vocabularies.find((v) => v.name === profile)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => { api.projects().then(setProjects).catch((e) => setError(e.message)) }, [])

  const create = async (sample: boolean) => {
    setBusy(true)
    setError(null)
    try {
      const p = sample ? await api.createSample() : await api.createProject(name.trim(), profile)
      await openProject(p.id)
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }

  return (
    <div className="picker">
      <h1>Modeling workbench</h1>
      <p className="muted">Build and correct a knowledge-graph model of your system · BuildingMOTIF skill {status.skill_version}</p>
      <div className="picker-new">
        <input placeholder="New project name" value={name} onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && name.trim() && create(false)} />
        <select value={profile} onChange={(e) => setProfile(e.target.value)} title="Ontology the model will use">
          {status.vocabularies.map((v) => (
            <option key={v.name} value={v.name} disabled={!!v.error}>{v.label}{v.error ? ' (unavailable)' : ''}</option>
          ))}
        </select>
        <button className="primary" disabled={!name.trim() || busy} onClick={() => create(false)}>Create</button>
      </div>
      {chosen && <p className="muted small profile-note">
        {chosen.description} {chosen.ready ? `${chosen.terms} terms loaded.` : chosen.loading
          ? 'Loading this vocabulary (the first time takes about a minute)…' : chosen.error ? `Unavailable: ${chosen.error}` : ''}
        {chosen.missing_imports.length > 0 && ` (${chosen.missing_imports.length} imports are not published online and were skipped.)`}
      </p>}
      {busy && <p className="muted small"><span className="spinner" /> Creating the project…</p>}
      <p><button className="link" disabled={busy} onClick={() => create(true)} title="A small RO train with a few deliberate mistakes">
        Or open the sample project (WaTr RO train with deliberate mistakes)</button></p>
      {error && <p className="error-text">{error}</p>}
      <h2>Projects</h2>
      {!projects ? <p className="muted">Loading…</p> : projects.length === 0 ? <p className="muted">No projects yet.</p> : (
        <ul className="project-list">
          {projects.map((p) => (
            <li key={p.id}>
              <button className="link" disabled={!!p.error} onClick={() => void openProject(p.id)}>{p.name}</button>
              {!p.error && <span className={`profile-badge ${p.family}`}>{p.profile_label?.split(' (')[0]}</span>}
              <span className="muted"> {p.error ? `cannot open: ${p.error}` : `${p.head} · ${new Date(p.updated_at).toLocaleString()}`}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
