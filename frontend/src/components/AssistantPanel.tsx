import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { summarize } from '../selection'
import { useStore } from '../store'
import type { AgentRun, Proposal, ProviderHealth } from '../types'

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
  const status = useStore((s) => s.status)
  const provider = useStore((s) => s.provider)
  const setProvider = useStore((s) => s.setProvider)
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [submittedReply, setSubmittedReply] = useState<string | null>(null)
  const [health, setHealth] = useState<ProviderHealth | null>(null)

  useEffect(() => {
    if (submittedReply && proposal?.parent_proposal_id === submittedReply) {
      setText('')
      setSubmittedReply(null)
    }
  }, [proposal?.id, proposal?.parent_proposal_id, submittedReply])

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
  const summary = summarize(selection, rows)
  const chosen = [...selection.entity_ids, ...selection.relationship_ids].map((id) => rows.get(id)).filter(Boolean)

  const send = async () => {
    if (!text.trim() || running || sending) return
    setSending(true)
    try {
      const replyingTo = proposal?.status === 'pending' ? proposal.id : null
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
      <textarea
        placeholder={proposal?.status === 'pending'
          ? 'Reply to this proposal, e.g. “A2 is also a VAV.”'
          : chosen.length ? 'Describe the correction, e.g. "These belong to RO-1."'
            : 'Select items in a table or the graph, then describe the correction.'}
        value={text} onChange={(e) => setText(e.target.value)} rows={3}
        onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) void send() }}
      />
      <div className="assist-actions">
        <select value={provider ?? ''} onChange={(e) => setProvider(e.target.value)} title="Model endpoint">
          {status?.providers.map((p) => (
            <option key={p.name} value={p.name} disabled={p.has_key === false}>
              {p.name} · {p.model}{p.has_key === false ? ' (no key)' : ''}
            </option>
          ))}
        </select>
        <button className="primary" disabled={!text.trim() || !!running || sending} onClick={() => void send()}>
          {proposal?.status === 'pending' ? 'Reply to proposal' : 'Propose change'}
        </button>
      </div>
      {health && (
        <div className={`provider-health ${health.ok ? 'ok' : 'bad'}`}>
          {health.ok ? `● connected · ${shortModel(health.model ?? '')}` : `● ${health.detail}`}
        </div>
      )}
      {run && <RunStatus run={run} />}
      {proposal && <ProposalPreview proposal={proposal} running={!!running || sending} />}
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
  const changeTable = <table className="changes">
    <thead><tr><th>Object</th><th>Field</th><th>Before</th><th>After</th></tr></thead>
    <tbody>
      {proposal.changes.flatMap((c) => (c.fields.length ? c.fields : [{ field: c.change, before: null, after: null }]).map((f, i) => (
        <tr key={`${c.entity_id}-${f.field}-${i}`} className={c.in_selection ? '' : 'outside'}
            onClick={(e) => { if (c.change !== 'deleted') click({ id: c.entity_id }, { ctrl: e.ctrlKey || e.metaKey, shift: false }, []); inspect(c.entity_id) }}>
          <td>{i === 0 && <>
            <span className={`change-kind ${c.change}`}>{c.change}</span> {c.label}
            {!c.in_selection && <span className="badge" title="Outside your selection">outside selection</span>}
          </>}</td>
          <td>{FIELD_LABELS[f.field] ?? f.field}
            {c.overrides_locked.includes(f.field) && <span className="badge warn" title="A person set this value earlier">overrides earlier edit</span>}
          </td>
          <td className="before">{fmt(f.before)}</td>
          <td className="after">{fmt(f.after)}</td>
        </tr>
      )))}
    </tbody>
  </table>

  return (
    <div className={`proposal status-${proposal.status}`}>
      <div className="proposal-head">
        <h3>Proposed change</h3>
        <span className="muted">based on {proposal.base_revision}</span>
      </div>
      {proposal.explanation && !proposal.conversation?.length && <p className="explanation">{proposal.explanation}</p>}
      {!!proposal.conversation?.length && <div className="proposal-conversation">
        {proposal.conversation.map((message, i) => <p key={i} className={message.role}>
          <strong>{message.role === 'user' ? 'You' : 'Assistant'}:</strong> {message.text}
        </p>)}
      </div>}
      {proposal.build_summary && (() => { const b = proposal.build_summary!; return (
        <div className="build-summary">
          <strong>{b.title}</strong>
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
        </div>) })()}
      {proposal.questions.length > 0 && proposal.questions.map((q, i) => <p key={i} className="question">? {q}</p>)}

      {proposal.build_summary ? <details>
        <summary>Individual changes ({proposal.changes.length})</summary>
        {changeTable}
      </details> : changeTable}
      {outside.length > 0 && <p className="warn-text">{outside.length} change(s) fall outside your selection — check them before applying.</p>}
      {proposal.notes.map((n, i) => <p key={i} className="warn-text">Note: {n}</p>)}

      {v && (
        <div className="validation-delta">
          <span>Model check: {v.before.violations} → <strong>{v.after.violations}</strong> problem(s)</span>
          {proposal.build_summary && v.resolved.length + v.introduced.length > 8
            ? <details><summary>Validation changes ({v.resolved.length + v.introduced.length})</summary>
                {validationFindings}</details>
            : validationFindings}
        </div>
      )}

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
        {proposal.status === 'pending' && <>
          <button className="primary" disabled={busy || running || proposal.operations.length === 0} onClick={() => void applyProposal()}>Apply</button>
          <button disabled={running} onClick={() => void dismissProposal()}>Dismiss</button>
        </>}
        {proposal.status === 'stale' && <>
          <span className="warn-text">The model is now at {head}; this proposal was made against {proposal.base_revision}.</span>
          <button className="primary" onClick={() => void regenerate()}>Regenerate</button>
          <button onClick={() => void dismissProposal()}>Dismiss</button>
        </>}
        {proposal.status === 'applied' && <span className="ok-text">Applied as {proposal.applied_revision}.</span>}
      </div>
    </div>
  )
}
