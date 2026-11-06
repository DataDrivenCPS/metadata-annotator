import { useState } from 'react'
import { citationLabel, excerpt, isGenerated } from '../citations'
import { useStore } from '../store'
import type { Citation } from '../types'

const GENERATED_TITLE = 'A vision model wrote this description of the page. It is not text from the source: check it against the page.'

export function GeneratedBadge() {
  return <span className="badge warn" title={GENERATED_TITLE}>model's description</span>
}

/** One source citation: its link opens the source at the cited place in the sources tray. */
export function CitationItem({ citation, about }: { citation: Citation; about?: string | null }) {
  const openCitation = useStore((s) => s.openCitation)
  const [all, setAll] = useState(false)
  const { content } = citation
  const text = typeof content.text === 'string' ? content.text : typeof content.name === 'string' ? content.name : ''
  const short = excerpt(text)
  return <div className="evidence-item">
    <button className="link citation-link" onClick={() => openCitation(citation, about)}>{citationLabel(citation)}</button>
    {isGenerated(citation) && <GeneratedBadge />}
    {text && <p className="evidence-excerpt">{all ? text : short.text}
      {short.truncated && <> <button className="link" onClick={() => setAll(!all)}>{all ? 'Show less' : 'Show all'}</button></>}
    </p>}
    {Object.keys(content.metadata ?? {}).length > 0 && <p className="muted">{JSON.stringify(content.metadata)}</p>}
  </div>
}
