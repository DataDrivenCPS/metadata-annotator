import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { summarize } from '../selection'
import { useStore } from '../store'
import type { AgentRun, Proposal, ProviderHealth } from '../types'
import { ResizeHandle } from './ResizeHandle'

const FIELD_LABELS: Record<string, string> = {
  label: 'Name', equipment: 'Equipment', point_kind: 'Kind', point_type: 'Point type', quantity_kind: 'Measurement', unit: 'Unit',
  sensor_type: 'Sensor type', medium: 'Medium', substance: 'Substance', type: 'Type', process: 'Process',
  contained_in: 'Part of', from_equipment: 'From', to_equipment: 'Connected to',
}

export function AssistantPanel() {
  const selection = useStore((s) => s.selection)
  const rows = useStore((s) => s.rows)
  const clearSelection = useStore((s) => s.clearSelection)
  const assist = useStore((s) => s.assist)
  const replyToProposal = useStore((s) => s.replyToProposal)
  const runs = useStore((s) => s.runs)
  const activeRunId = useStore((s) => s.activeRunId)
  const proposal = useStore((s) => s.proposal)
  const text = useStore((s) => s.assistantDraft)
  const setText = useStore((s) => s.setAssistantDraft)
  const draftVersion = useStore((s) => s.assistantDraftVersion)
  const replyingTo = useStore((s) => s.assistantReplyToId)
  const setReplyingTo = useStore((s) => s.setAssistantReplyTo)
  const status = useStore((s) => s.status)
  const provider = useStore((s) => s.provider)
  const setProvider = useStore((s) => s.setProvider)
  const [sending, setSending] = useState(false)
  const [submittedReply, setSubmittedReply] = useState<string | null>(null)
  const [health, setHealth] = useState<ProviderHealth | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const [proposalHeight, setProposalHeight] = useState(() => {
    const stored = Number(localStorage.getItem('workbench.proposalHeight'))
    return Number.isFinite(stored) && stored >= 140 ? stored : 360
  })
  const resizeProposal = (delta: number) => setProposalHeight((height) => {
    const next = Math.max(140, Math.min(window.innerHeight - 260, height + delta))
    localStorage.setItem('workbench.proposalHeight', String(next))
    return next
  })

  useEffect(() => {
    if (submittedReply && proposal?.parent_proposal_id === submittedReply) {
      setText('')
      setSubmittedReply(null)
    }
  }, [proposal?.id, proposal?.parent_proposal_id, setText, submittedReply])

  useEffect(() => {
    if (!draftVersion) return
    const textarea = textareaRef.current
    if (textarea) {
      textarea.focus()
      textarea.setSelectionRange(textarea.value.length, textarea.value.length)
    }
  }, [draftVersion])

  useEffect(() => {
    if (!provider) return
    let alive = true
    const check = () => api.providerHealth(provider).then((h) => alive && setHealth(h)).catch(() => alive && setHealth(null))
    setHealth(null)
    void check()
    const t = setInterval(check, 15000)
    return () => { alive = false; clearInterval(t) }
  }, [provider])

  const run = activeRunId ? runs[activeRunId] : null
  const running = run && (run.status === 'queued' || run.status === 'running')
  const replying = proposal?.status === 'pending' && replyingTo === proposal.id
  const summary = summarize(selection, rows)
  const chosen = [...selection.entity_ids, ...selection.relationship_ids].map((id) => rows.get(id)).filter(Boolean)

  const send = async (replying: boolean) => {
    if (!text.trim() || running || sending) return
    setSending(true)
    try {
      const replyingTo = replying && proposal?.status === 'pending' ? proposal.id : null
      const sent = replyingTo
        ? await replyToProposal(text.trim()) : await assist(text.trim())
      if (sent && replyingTo) setSubmittedReply(replyingTo)
      else if (sent) setText('')
    } finally {
      setSending(false)
    }
  }

  return (
    <aside className="assistant">
      <div className="assistant-main">
      <h2>Assistant</h2>
      <div className="selection-box">
        <div className="selection-summary">
          <strong>{summary}</strong>
          {chosen.length > 0 && <button className="link" onClick={clearSelection}>clear</button>}
        </div>
        {chosen.length > 0 && (
          <div className="chips">
            {chosen.slice(0, 12).map((r) => <span key={r!.id} className={`chip ${r!.kind}`}>{r!.label}</span>)}
            {chosen.length > 12 && <span className="muted">+{chosen.length - 12} more</span>}
          </div>
        )}
      </div>
      {proposal?.status === 'pending' && <div className="message-mode" role="group" aria-label="Message mode">
        <button className={!replying ? 'active' : ''} onClick={() => setReplyingTo(null)}>New change</button>
        <button className={replying ? 'active' : ''} onClick={() => setReplyingTo(proposal.id)}>Reply to proposal</button>
      </div>}
      {replying && proposal?.status === 'pending' && <div className="reply-context">
        <span>Replying to the assistant’s proposed change</span>
        <button className="link" onClick={() => setReplyingTo(null)}>switch to new change</button>
      </div>}
      <textarea
        ref={textareaRef}
        placeholder={replying
          ? 'Tell the assistant what to revise, e.g. “A2 is also a VAV.”'
          : chosen.length ? 'Describe a new change, e.g. “These belong to RO-1.”'
            : 'Select items in a table or the graph, then describe a new change.'}
        value={text} onChange={(e) => setText(e.target.value)} rows={3}
        onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) void send(replying) }}
      />
      <div className="assist-actions">
        <select value={provider ?? ''} onChange={(e) => setProvider(e.target.value)} title="Model endpoint">
          {status?.providers.map((p) => (
            <option key={p.name} value={p.name} disabled={p.has_key === false}>
              {p.name} · {p.model}{p.has_key === false ? ' (no key)' : ''}
            </option>
          ))}
        </select>
        <button className="primary" disabled={!text.trim() || !!running || sending}
          onClick={() => void send(replying)}>
          {replying ? 'Send reply' : 'Propose change'}
        </button>
      </div>
      {health && (
        <div className={`provider-health ${health.ok ? 'ok' : 'bad'}`}>
          {health.ok ? `● connected · ${shortModel(health.model ?? '')}` : `● ${health.detail}`}
        </div>
      )}
      {run && <RunStatus run={run} />}
      </div>
      {proposal && <>
        <ResizeHandle label="Resize proposed change panel" onResize={resizeProposal} />
        <div className="proposal-region" style={{ height: proposalHeight }}>
          <ProposalPreview proposal={proposal} running={!!running || sending} />
        </div>
      </>}
    </aside>
  )
}

const shortModel = (m: string) => m.split(/[\\/]/).pop() ?? m

function RunStatus({ run }: { run: AgentRun }) {
  const cancelRun = useStore((s) => s.cancelRun)
  const active = run.status === 'queued' || run.status === 'running'
  const [open, setOpen] = useState(false)
  const last = run.progress[run.progress.length - 1]
  return (
    <div className={`run run-${run.status}`}>
      <div className="run-head">
        {active && <span className="spinner" />}
        <span>{active ? (last?.message ?? 'Starting…') : run.status === 'succeeded'
          ? (run.outcome.proposal_id ? 'Proposal ready' : run.outcome.questions?.length ? 'The assistant has questions' : 'No change proposed')
          : run.status === 'cancelled' ? 'Cancelled' : 'Failed'}</span>
        <span className="spacer" />
        {active && <button onClick={() => void cancelRun()}>Cancel</button>}
        <button className="link" onClick={() => setOpen(!open)}>{open ? 'hide steps' : 'steps'}</button>
      </div>
      {run.error && <div className="error-text">{run.error}</div>}
      {run.status === 'succeeded' && !run.outcome.proposal_id && (
        <div className="questions">
          {run.outcome.explanation && <p>{run.outcome.explanation}</p>}
          {run.outcome.questions?.map((q, i) => <p key={i} className="question">? {q}</p>)}
        </div>
      )}
      {open && (
        <ol className="steps">
          {run.progress.map((p, i) => <li key={i}><span className="stage">{p.stage}</span> {p.message}</li>)}
          <li className="muted">{run.provider} · {run.model} · skill {run.skill_version}
            {run.outcome.input_tokens ? ` · ${run.outcome.input_tokens}+${run.outcome.output_tokens} tokens` : ''}</li>
        </ol>
      )}
    </div>
  )
}

function fmt(v: unknown) {
  if (v === null || v === undefined || v === '') return <span className="muted">—</span>
  return String(v)
}

function ProposalPreview({ proposal, running }: { proposal: Proposal; running: boolean }) {
  const applyProposal = useStore((s) => s.applyProposal)
  const dismissProposal = useStore((s) => s.dismissProposal)
  const regenerate = useStore((s) => s.regenerate)
  const click = useStore((s) => s.click)
  const inspect = useStore((s) => s.inspect)
  const busy = useStore((s) => s.busy)
  const head = useStore((s) => s.model?.head)
  const v = proposal.validation
  const outside = useMemo(() => proposal.changes.filter((c) => !c.in_selection), [proposal])
  const validationFindings = v && <>
    {v.resolved.map((r, i) => <div key={`r${i}`} className="resolved">✓ fixes: {r}</div>)}
    {v.introduced.map((r, i) => <div key={`n${i}`} className="introduced">! new: {r}</div>)}
  </>
  const changeDetails = <div className="change-accordions">
    {proposal.changes.map((change) => {
      const fields = change.fields.length ? change.fields : [{ field: change.change, before: null, after: null }]
      return <details key={change.entity_id} className={change.in_selection ? '' : 'outside'}>
        <summary>
          <span className={`change-kind ${change.change}`}>{change.change}</span> {change.label}
          <span className="muted"> · {fields.length} field(s)</span>
          {!change.in_selection && <span className="badge" title="Outside your selection">outside selection</span>}
        </summary>
        <table className="changes">
          <thead><tr><th>Field</th><th>Before</th><th>After</th></tr></thead>
          <tbody>{fields.map((field, index) => (
            <tr key={`${field.field}-${index}`} onClick={(event) => {
              if (change.change !== 'deleted') click({ id: change.entity_id },
                { ctrl: event.ctrlKey || event.metaKey, shift: false }, [])
              inspect(change.entity_id)
            }}>
              <td>{FIELD_LABELS[field.field] ?? field.field}
                {change.overrides_locked.includes(field.field) && <span className="badge warn" title="A person set this value earlier">overrides earlier edit</span>}
              </td>
              <td className="before">{fmt(field.before)}</td>
              <td className="after">{fmt(field.after)}</td>
            </tr>
          ))}</tbody>
        </table>
      </details>
    })}
  </div>

  return (
    <div className={`proposal status-${proposal.status}`}>
      <div className="proposal-head">
        <h3>Assistant · proposed change</h3>
        <span className="muted">based on {proposal.base_revision}</span>
      </div>
      <div className="proposal-conversation">
        {proposal.conversation?.length ? proposal.conversation.map((message, i) => <p key={i} className={message.role === 'user' ? 'user' : 'agent'}>
          <strong>{message.role === 'user' ? 'You' : 'Assistant'}:</strong> {message.text}
        </p>) : <>
          {proposal.instruction && <p className="user"><strong>You:</strong> {proposal.instruction}</p>}
          {proposal.explanation && <p className="agent"><strong>Assistant:</strong> {proposal.explanation}</p>}
        </>}
      </div>
      {proposal.build_summary && (() => { const b = proposal.build_summary!; return <details>
        <summary>Source build · {b.title} · {b.records} records</summary>
        <div className="build-summary">
          {b.revision_note && <p className="muted small">{b.revision_note}</p>}
          <p className="muted small">Read {Math.round(b.parse.coverage * 100)}% of records using {b.parse.description}.
            Mapped {b.mapped_tokens} of {b.token_count} point tokens{b.unmapped_records ? `; ${b.unmapped_records} records left unresolved` : ''}.</p>
          <div className="grid-scroll short"><table className="raw-grid">
            <thead><tr><th>#</th><th>Token</th><th>Maps to</th></tr></thead>
            <tbody>{b.point_mappings.map((m) => <tr key={m.id}><td>{m.count}</td><td className="name">{m.token}</td>
              <td>{m.term_label ?? (m.point_kind ?? <span className="warn-text">unmapped</span>)}</td></tr>)}
            {b.equipment_mappings.map((m) => <tr key={m.id}><td>{m.count}</td><td>{m.examples.slice(0, 2).join(', ')}</td>
              <td>{m.term_label ?? <span className="warn-text">unclassified</span>}</td></tr>)}</tbody>
          </table></div>
        </div>
      </details> })()}

      <details>
        <summary>Changes · {proposal.changes.length} object(s)</summary>
        {outside.length > 0 && <p className="warn-text">{outside.length} change(s) fall outside your selection — check them before applying.</p>}
        {proposal.changes.length ? changeDetails : <p className="muted">No model changes were needed.</p>}
      </details>

      {(proposal.questions.length > 0 || proposal.notes.length > 0) && <details>
        <summary>Assistant notes and questions · {proposal.questions.length + proposal.notes.length}</summary>
        {proposal.questions.map((q, i) => <p key={`q${i}`} className="question">? {q}</p>)}
        {proposal.notes.map((n, i) => <p key={`n${i}`} className="warn-text">Note: {n}</p>)}
      </details>}

      {v && <details>
        <summary>Model checks · {v.before.violations} → {v.after.violations} problem(s)
          {v.resolved.length + v.introduced.length ? ` · ${v.resolved.length + v.introduced.length} changed finding(s)` : ''}</summary>
        <div className="validation-delta">{validationFindings}</div>
      </details>}

      <details>
        <summary>Evidence ({proposal.evidence.length})</summary>
        <ul className="evidence">
          {proposal.evidence.map((e, i) => (
            <li key={i}><span className={`ev-kind ${e.kind}`}>{e.kind}</span> <code>{e.ref}</code> {e.summary}</li>
          ))}
        </ul>
      </details>
      <details>
        <summary>Technical detail ({proposal.operations.length} operation(s), +{proposal.diff.added.length}/−{proposal.diff.removed.length} triples)</summary>
        <pre className="code">{JSON.stringify(proposal.operations, null, 1)}</pre>
        <pre className="code diff">
          {proposal.diff.removed.map((t) => `- ${t}`).join('\n')}{'\n'}{proposal.diff.added.map((t) => `+ ${t}`).join('\n')}
        </pre>
      </details>

      <div className="proposal-actions">
        {(proposal.status === 'pending' || proposal.status === 'stale') && <>
          {proposal.status === 'stale' && <span className="warn-text">
            The model is now at {head}. Applying replays these operations and may overwrite newer values.
          </span>}
          <button className="primary" disabled={busy || running || proposal.operations.length === 0}
            onClick={() => void applyProposal()}>Apply as proposed</button>
          <button disabled={busy || running} onClick={() => void regenerate()}>Refresh on latest</button>
          <button disabled={busy || running} onClick={() => void dismissProposal()}>Dismiss</button>
        </>}
        {proposal.status === 'applied' && <span className="ok-text">Applied as {proposal.applied_revision}.</span>}
      </div>
    </div>
  )
}
