import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { continuation, describeStep, effectiveSelection, formatDuration, isActive, startsExchange, threadRuns,
  type ProposalState } from '../assistant'
import { summarize } from '../selection'
import { useStore } from '../store'
import type { AgentRun, Proposal, ProviderHealth } from '../types'

const FIELD_LABELS: Record<string, string> = {
  label: 'Name', equipment: 'Equipment', point_kind: 'Kind', point_type: 'Point type', quantity_kind: 'Measurement', unit: 'Unit',
  sensor_type: 'Sensor type', medium: 'Medium', substance: 'Substance', type: 'Type', process: 'Process',
  contained_in: 'Part of', from_equipment: 'From', to_equipment: 'Connected to',
}

const CONTINUE_LABEL = {
  proposal: 'Replying to the proposed change',
  questions: 'Answering the assistant’s questions',
  conversation: 'Continuing the conversation',
}

export function AssistantPanel() {
  const runs = useStore((s) => s.runs)
  const proposal = useStore((s) => s.proposal)
  const newRequest = useStore((s) => s.assistantNewRequest)
  const thread = useMemo(() => threadRuns(runs), [runs])
  const last = thread[thread.length - 1]
  const orphan = proposal && !thread.some((r) => r.outcome.proposal_id === proposal.id) ? proposal : null
  const threadRef = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)

  // Follow new messages and progress while the reader is at the bottom of the thread.
  useLayoutEffect(() => {
    const el = threadRef.current
    if (el && pinned.current) el.scrollTop = el.scrollHeight
  }, [thread.length, last?.status, last?.progress.length, proposal?.id, proposal?.status])

  return (
    <aside className="assistant">
      <div className="assistant-head"><h2>Assistant</h2><ModelStatus /></div>
      <div className="thread" ref={threadRef} onScroll={(e) => {
        const el = e.currentTarget
        pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
      }}>
        {!thread.length && !orphan && <Starters />}
        {thread.map((run, i) => <div key={run.id} className="exchange">
          {i > 0 && startsExchange(run) && <div className="thread-divider"><span>New request</span></div>}
          <UserMessage run={run} />
          <AssistantMessage run={run} isLast={run === last && !newRequest} />
        </div>)}
        {orphan && <ProposalPreview proposal={orphan} running={!!last && isActive(last)} showConversation />}
      </div>
      <Composer />
    </aside>
  )
}

function Composer() {
  const selection = useStore((s) => s.selection)
  const rows = useStore((s) => s.rows)
  const clearSelection = useStore((s) => s.clearSelection)
  const sendMessage = useStore((s) => s.sendMessage)
  const runs = useStore((s) => s.runs)
  const proposal = useStore((s) => s.proposal)
  const text = useStore((s) => s.assistantDraft)
  const setText = useStore((s) => s.setAssistantDraft)
  const draftVersion = useStore((s) => s.assistantDraftVersion)
  const newRequest = useStore((s) => s.assistantNewRequest)
  const setNewRequest = useStore((s) => s.setAssistantNewRequest)
  const [sending, setSending] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    if (!draftVersion) return
    const textarea = textareaRef.current
    if (textarea) {
      textarea.focus()
      textarea.setSelectionRange(textarea.value.length, textarea.value.length)
    }
  }, [draftVersion])

  const thread = threadRuns(runs)
  const last = thread[thread.length - 1]
  const running = !!last && isActive(last)
  const next = continuation(thread, proposal, newRequest)
  const canContinue = continuation(thread, proposal, false) !== null
  const { selection: about, source } = effectiveSelection(next, selection, thread, proposal)
  const summary = summarize(about, rows)
  const chosen = [...about.entity_ids, ...about.relationship_ids].map((id) => rows.get(id)).filter(Boolean)

  const send = async () => {
    if (!text.trim() || running || sending) return
    setSending(true)
    try {
      if (await sendMessage(text.trim())) setText('')
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="composer">
      <div className="composer-selection" title={source === 'conversation'
        ? 'Nothing is selected, so this message stays about what the conversation was about. Select something to change it.'
        : source === 'proposal' ? 'A reply revises the proposal, so it keeps the proposal’s selection.' : undefined}>
        <span className="muted">{source === 'current' ? 'Selected:'
          : source === 'conversation' ? 'About (from the conversation):' : 'About (the proposal’s selection):'}</span>
        {chosen.length ? <>
          {chosen.slice(0, 6).map((r) => <span key={r!.id} className={`chip ${r!.kind}`}>{r!.label}</span>)}
          {chosen.length > 6 && <span className="muted">+{chosen.length - 6} more</span>}
          {source === 'current' && <button className="link" onClick={clearSelection}>clear</button>}
        </> : <span className="muted">{summary}</span>}
      </div>
      {canContinue && <div className={`composer-mode mode-${next ? next.kind : 'new'}`}>
        {next ? <>
          <span>↩ {CONTINUE_LABEL[next.kind]}</span>
          <button className="link" onClick={() => setNewRequest(true)}>Start a new request</button>
        </> : <>
          <span>New request — the assistant won’t see the earlier conversation</span>
          <button className="link" onClick={() => setNewRequest(false)}>Continue instead</button>
        </>}
      </div>}
      <textarea
        ref={textareaRef}
        placeholder={next?.kind === 'proposal' ? 'Tell the assistant what to revise, e.g. “A2 is also a VAV.”'
          : next?.kind === 'questions' ? 'Answer the assistant’s questions…'
            : chosen.length ? 'Describe a change, e.g. “These belong to RO-1.”'
              : 'Select items in a table or the graph, then describe a change.'}
        value={text} onChange={(e) => setText(e.target.value)} rows={3}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send() }
        }}
      />
      <div className="assist-actions">
        <span className="muted small">Enter to send · Shift+Enter for a new line</span>
        <button className="primary" disabled={!text.trim() || running || sending} onClick={() => void send()}
          title="Enter to send · Shift+Enter for a new line">
          {next?.kind === 'proposal' ? 'Send reply' : 'Send'}
        </button>
      </div>
    </div>
  )
}

const shortModel = (m: string) => m.split(/[\\/]/).pop() ?? m

/** The model endpoint the assistant uses, and whether it can be reached right now. */
function ModelStatus() {
  const status = useStore((s) => s.status)
  const provider = useStore((s) => s.provider)
  const setProvider = useStore((s) => s.setProvider)
  const [health, setHealth] = useState<ProviderHealth | null>(null)
  const [checkVersion, setCheckVersion] = useState(0)

  useEffect(() => {
    if (!provider) return
    let alive = true
    const check = () => api.providerHealth(provider).then((h) => alive && setHealth(h))
      .catch((e) => alive && setHealth({ ok: false, detail: `Could not check the model endpoint: ${(e as Error).message}` }))
    void check()
    const t = setInterval(check, 15000)
    return () => { alive = false; clearInterval(t) }
  }, [provider, checkVersion])

  const state = !health ? 'checking' : health.ok ? 'ok' : 'bad'
  const current = status?.providers.find((p) => p.name === provider)
  return <>
    <div className="model-status">
      <span className={`status-pill ${state}`} title={health?.ok
        ? `Connected to ${current?.base_url ?? provider} · ${shortModel(health.model ?? current?.model ?? '')}`
        : health?.detail ?? 'Checking the model endpoint…'}>
        <span className="dot" />{state === 'checking' ? 'Checking…' : state === 'ok' ? 'Connected' : 'Unavailable'}
      </span>
      <select value={provider ?? ''} title="Model endpoint" onChange={(e) => {
        setHealth(null)
        setProvider(e.target.value)
      }}>
        {status?.providers.map((p) => (
          <option key={p.name} value={p.name} disabled={p.has_key === false}>
            {p.name} · {shortModel(p.model)}{p.has_key === false ? ' (no key)' : ''}
          </option>
        ))}
      </select>
    </div>
    {state === 'bad' && <div className="model-status-detail">
      <span>{health!.detail} Requests can’t run until it is reachable.</span>
      <button className="link" onClick={() => { setHealth(null); setCheckVersion((v) => v + 1) }}>Check again</button>
    </div>}
  </>
}

const STARTER_ISSUES = 10

/** One-click ways to start a conversation; each fills the message box for review first. */
function Starters() {
  const issues = useStore((s) => s.model?.issues)
  const selection = useStore((s) => s.selection)
  const addIssuesToPrompt = useStore((s) => s.addIssuesToPrompt)
  const appendAssistantContext = useStore((s) => s.appendAssistantContext)
  const violations = (issues ?? []).filter((i) => i.resolution_state === 'open' && i.severity === 'violation')
  const selected = selection.entity_ids.length + selection.relationship_ids.length > 0
  const starters = [
    violations.length > 0 && {
      label: violations.length > STARTER_ISSUES ? `Fix the first ${STARTER_ISSUES} of ${violations.length} open violations`
        : `Fix the ${violations.length} open violation${violations.length === 1 ? '' : 's'}`,
      hint: 'Selects the affected objects and lists the validator findings',
      run: () => addIssuesToPrompt(violations.slice(0, STARTER_ISSUES)),
    },
    selected && {
      label: 'Explain the selection',
      hint: 'What these objects are, how they are modeled, and what looks off',
      run: () => appendAssistantContext('Explain what the selected objects are and how they are modeled, and point out anything that looks wrong or incomplete.'),
    },
    {
      label: 'What is missing from this model?',
      hint: 'Equipment, points, or connections that should probably exist',
      run: () => appendAssistantContext('Review the model for missing equipment, points, or connections. Summarize what should be added, and ask me about anything you cannot tell from the model.'),
    },
  ].filter((x) => !!x)
  return <div className="thread-empty">
    <p className="muted">Select rows in a table or the graph, then describe what is wrong or what to add. The assistant
      replies with an explanation, questions, or a proposed change you review before it is applied.</p>
    <div className="starters">
      <span className="muted small">Or start with</span>
      {starters.map((st) => <button key={st.label} onClick={st.run} title={st.hint}>{st.label}</button>)}
    </div>
  </div>
}

/** Ticks once a second while ``active``, for live elapsed times. */
function useNow(active: boolean) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [active])
  return active ? now : 0
}

/** The run's steps in plain words with how long each took; the last one is live while running. */
function RunSteps({ run, live }: { run: AgentRun; live: boolean }) {
  const now = useNow(live)
  const [showAll, setShowAll] = useState(false)
  const steps = run.progress
  const end = live ? now : Date.parse(run.finished_at ?? run.created_at)
  const durations = steps.map((p, i) => (i + 1 < steps.length ? Date.parse(steps[i + 1].at) : end) - Date.parse(p.at))
  const hidden = live && !showAll ? Math.max(0, steps.length - 5) : 0
  const current = live ? steps[steps.length - 1] : undefined
  const slowModel = current?.stage === 'model' && durations[durations.length - 1] > 20000
  return <>
    <ol className="steps run-steps">
      {hidden > 0 && <li className="muted"><button className="link small" onClick={() => setShowAll(true)}>
        {hidden} earlier step{hidden === 1 ? '' : 's'}</button></li>}
      {steps.slice(hidden).map((p, i) => {
        const index = i + hidden
        const isCurrent = live && index === steps.length - 1
        return <li key={index} className={isCurrent ? 'current' : p.stage === 'rejected' ? 'rejected' : 'done'}>
          <span className="step-mark">{isCurrent ? <span className="spinner small" /> : p.stage === 'rejected' ? '↻' : '✓'}</span>
          <span className="step-text">{describeStep(p)}</span>
          <span className="step-time">{formatDuration(durations[index])}</span>
        </li>
      })}
      {live && !steps.length && <li className="current"><span className="step-mark"><span className="spinner small" /></span>
        <span className="step-text">{run.status === 'queued' ? 'Waiting to start…' : 'Starting…'}</span></li>}
      {!live && <li className="run-meta muted">{run.provider} · {run.model} · skill {run.skill_version}
        {run.outcome.input_tokens ? ` · ${run.outcome.input_tokens}+${run.outcome.output_tokens} tokens` : ''}</li>}
    </ol>
    {slowModel && <div className="muted small step-note">The model is still working. Self-hosted models can take a
      minute or more per step; you can keep browsing while you wait.</div>}
  </>
}

function UserMessage({ run }: { run: AgentRun }) {
  const rows = useStore((s) => s.rows)
  if (run.mode === 'reconsider') return <div className="thread-event">↻ You asked the assistant to refresh the proposal on the latest model</div>
  const text = run.mode === 'build'
    ? `Build a model from ${run.source_ids?.length ?? 0} source(s)${run.instruction ? `: ${run.instruction}` : '.'}`
    : run.instruction
  const about = [...(run.selection?.entity_ids ?? []), ...(run.selection?.relationship_ids ?? [])]
    .map((id) => rows.get(id)?.label).filter(Boolean)
  return (
    <div className="msg user">
      <div className="msg-author">You</div>
      <LongText text={text} />
      {about.length > 0 && <div className="msg-about">about {about.slice(0, 4).join(', ')}{about.length > 4 ? ` +${about.length - 4}` : ''}</div>}
    </div>
  )
}

function LongText({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  const long = text.length > 420 || text.split('\n').length > 8
  return <>
    <div className={`msg-text ${long && !open ? 'clamped' : ''}`}>{text}</div>
    {long && <button className="link small" onClick={() => setOpen(!open)}>{open ? 'Show less' : 'Show more'}</button>}
  </>
}

function AssistantMessage({ run, isLast }: { run: AgentRun; isLast: boolean }) {
  const cancelRun = useStore((s) => s.cancelRun)
  const proposal = useStore((s) => s.proposal)
  const states = useStore((s) => s.proposalStates)
  const focusAssistant = useStore((s) => s.focusAssistant)
  const [open, setOpen] = useState(false)
  const active = isActive(run)
  const now = useNow(active)
  const elapsed = (active ? now : Date.parse(run.finished_at ?? run.created_at)) - Date.parse(run.created_at)
  const proposalId = run.status === 'succeeded' ? run.outcome.proposal_id : null
  const current = proposalId && proposal?.id === proposalId ? proposal : null
  const questions = !proposalId ? run.outcome.questions ?? [] : current?.questions ?? []

  return (
    <div className={`msg agent run-${run.status}`}>
      <div className="msg-author">Assistant
        {active && <span className="spinner" />}
        {active && <span className="run-elapsed" title="Time since the request started">{formatDuration(elapsed)}</span>}
        <span className="spacer" />
        {active && <button onClick={() => void cancelRun()}>Cancel</button>}
      </div>
      {active && <RunSteps run={run} live />}
      {run.status === 'failed' && <div className="error-text">{run.error ?? 'The run failed.'}</div>}
      {run.status === 'cancelled' && <div className="muted">Cancelled.</div>}
      {run.status === 'succeeded' && <>
        {run.outcome.explanation && <div className="msg-text">{run.outcome.explanation}</div>}
        {!run.outcome.explanation && !proposalId && !questions.length && <div className="muted">No change proposed.</div>}
        {run.outcome.dismissed_proposal_id && <div className="thread-event">The earlier proposal is no longer needed and was closed.</div>}
      </>}
      {questions.length > 0 && <div className="needs-input">
        <div className="needs-input-head"><span className="needs-input-icon">?</span> Needs your input</div>
        <ul>{questions.map((q, i) => <li key={i}>{q}</li>)}</ul>
        {isLast && <button className="primary" onClick={focusAssistant}>Answer below</button>}
      </div>}
      {current
        ? <ProposalPreview proposal={current} running={false} />
        : proposalId && <div className="proposal-ref">Proposed change · {proposalStateText(states[proposalId])}</div>}
      {!active && <>
        <div className="msg-foot">
          <button className="link" onClick={() => setOpen(!open)}>{open ? 'hide steps' : `${run.progress.length} steps`}</button>
          <span className="muted"> · took {formatDuration(elapsed)}</span>
        </div>
        {open && <RunSteps run={run} live={false} />}
      </>}
    </div>
  )
}

function proposalStateText(state: ProposalState | undefined) {
  switch (state) {
    case 'applied': return 'applied'
    case 'superseded': return 'revised below'
    case 'dismissed': return 'dismissed'
    case 'stale': return 'out of date'
    case 'pending': return 'set aside, not applied'
    default: return 'no longer open'
  }
}

function fmt(v: unknown) {
  if (v === null || v === undefined || v === '') return <span className="muted">—</span>
  return String(v)
}

function ProposalPreview({ proposal, running: runningProp, showConversation = false }: {
  proposal: Proposal; running: boolean; showConversation?: boolean
}) {
  const runs = useStore((s) => s.runs)
  const running = runningProp || Object.values(runs).some(isActive)
  const applyProposal = useStore((s) => s.applyProposal)
  const dismissProposal = useStore((s) => s.dismissProposal)
  const regenerate = useStore((s) => s.regenerate)
  const click = useStore((s) => s.click)
  const inspect = useStore((s) => s.inspect)
  const busy = useStore((s) => s.busy)
  const head = useStore((s) => s.model?.head)
  const v = proposal.validation
  const outside = useMemo(() => proposal.changes.filter((c) => !c.in_selection), [proposal])
  const dismissals = proposal.issue_dismissals ?? []
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
        <h3>Proposed change</h3>
        <span className="muted">based on {proposal.base_revision}</span>
      </div>
      {showConversation && <div className="proposal-conversation">
        {proposal.conversation?.length ? proposal.conversation.map((message, i) => <p key={i} className={message.role === 'user' ? 'user' : 'agent'}>
          <strong>{message.role === 'user' ? 'You' : 'Assistant'}:</strong> {message.text}
        </p>) : <>
          {proposal.instruction && <p className="user"><strong>You:</strong> {proposal.instruction}</p>}
          {proposal.explanation && <p className="agent"><strong>Assistant:</strong> {proposal.explanation}</p>}
        </>}
      </div>}
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

      {proposal.gate && <div className={`gate ${proposal.gate.sound ? 'sound' : 'unsound'}`}
        title="The repair engine's soundness gate (pyshifty): re-validates the model with this change and compares violations. Sound = introduces nothing; progress = fixes something.">
        <strong>Soundness gate:</strong> {proposal.gate.sound ? 'sound' : 'not sound'} · {proposal.gate.progress ? 'progress' : 'no progress'}
        {proposal.gate.fixed.length > 0 && <div>fixes: {proposal.gate.fixed.join('; ')}</div>}
        {proposal.gate.introduced.length > 0 && <div>introduces: {proposal.gate.introduced.join('; ')}</div>}
      </div>}

      {dismissals.length > 0 && <details className="dismissals" open>
        <summary>Issues to dismiss · {dismissals.length}</summary>
        <ul>{dismissals.map((d) => <li key={d.id}>
          <span className={`sev ${d.severity}`}>{d.severity}</span> {d.explanation}
          <div className="dismissal-reason">Why: {d.reason}</div>
        </li>)}</ul>
        <p className="muted small">Applying hides these from the open issues; the model itself does not change. You can reopen them from the Issues list.</p>
      </details>}

      {(proposal.changes.length > 0 || !dismissals.length) && <details>
        <summary>Changes · {proposal.changes.length} object(s)</summary>
        {outside.length > 0 && <p className="warn-text">{outside.length} change(s) fall outside your selection — check them before applying.</p>}
        {proposal.changes.length ? changeDetails : <p className="muted">No model changes were needed.</p>}
      </details>}

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
          <button className="primary" disabled={busy || running || (proposal.operations.length === 0 && !dismissals.length)}
            onClick={() => void applyProposal()}>{proposal.operations.length ? 'Apply as proposed'
              : `Dismiss ${dismissals.length} issue(s)`}</button>
          <button disabled={busy || running} onClick={() => void regenerate()}>Refresh on latest</button>
          <button disabled={busy || running} onClick={() => void dismissProposal()}>Discard</button>
        </>}
        {proposal.status === 'applied' && <span className="ok-text">{proposal.operations.length
          ? `Applied as ${proposal.applied_revision}.` : `Dismissed ${dismissals.length} issue(s).`}</span>}
      </div>
    </div>
  )
}
