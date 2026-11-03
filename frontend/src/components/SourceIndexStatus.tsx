import { api } from '../api'
import { useStore } from '../store'
import type { Source } from '../types'

/** Whether a document's passages are indexed for the assistant to search, in the source list. */
export function SourceIndexStatus({ source }: { source: Source }) {
  const projectId = useStore((s) => s.projectId)
  const notify = useStore((s) => s.notify)
  const index = source.index
  if (!index || !projectId) return null
  const retry = (e: React.MouseEvent) => {
    e.stopPropagation()
    api.reindexSource(projectId, source.id).catch((err: Error) => notify({ kind: 'error', text: `Could not reindex: ${err.message}` }))
  }
  const described = index.described
    ? `${index.described} drawing page${index.described === 1 ? '' : 's'} described by ${index.description_model}`
      + (index.description_tokens ? ` (${index.description_tokens.toLocaleString()} tokens)` : '')
    : null
  const detail = [index.message, index.parser === 'basic' ? 'Embedded text only' : null, described, index.note,
    index.warning, index.error].filter(Boolean).join(' · ')
  const text = index.status === 'queued' ? 'waiting to index'
    : index.status === 'indexing' ? (index.pages_total ? `indexing ${index.pages_done ?? 0}/${index.pages_total} pages` : 'indexing…')
    : index.status === 'ready' ? `${index.chunks ?? 0} passage${index.chunks === 1 ? '' : 's'}`
    : index.status === 'unsupported' ? 'not indexed' : 'indexing failed'
  return <span className={`src-index ${index.status}${index.warning ? ' warned' : ''}`} title={detail}>
    {text}{index.warning && index.status === 'ready' ? ' ⚠' : ''}
    {(index.status === 'failed' || index.status === 'unsupported' || (index.status === 'ready' && index.warning)) &&
      <button className="link" onClick={retry}>retry</button>}
  </span>
}
