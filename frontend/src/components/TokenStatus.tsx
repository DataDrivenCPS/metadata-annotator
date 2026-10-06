import { useEffect, useState } from 'react'
import { api } from '../api'
import { tokenTotals } from '../assistant'
import { useStore } from '../store'
import type { TokenUsage } from '../types'

export function TokenStatus({ projectId }: { projectId: string }) {
  const runs = useStore((s) => s.runs)
  const [recorded, setRecorded] = useState<Record<string, TokenUsage> | null>(null)
  const [error, setError] = useState(false)
  useEffect(() => {
    let current = true
    api.tokenUsage(projectId).then((usage) => { if (current) setRecorded(usage) })
      .catch(() => { if (current) setError(true) })
    return () => { current = false }
  }, [projectId])
  const totals = tokenTotals(recorded ?? {}, runs)
  return <span className="token-status" title="Provider-reported tokens for this project, including chat, source builds, and auto-fix. Updates after each model call; clearing chat keeps these totals. Providers that omit usage cannot be counted.">
    <span className="muted">Tokens</span>
    {error ? <span className="muted">unavailable</span> : !recorded ? <span className="muted">loading…</span> : <>
      <span>Sent <strong>{totals.input_tokens.toLocaleString()}</strong></span>
      <span>Received <strong>{totals.output_tokens.toLocaleString()}</strong></span>
    </>}
  </span>
}
