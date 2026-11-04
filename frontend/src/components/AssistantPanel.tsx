import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { autofixCandidates, callBreakdown, continuation, describeStep, effectiveSelection, formatDuration, isActive,
  issuesOnSelection, leftOutSummary, owningRun, runOptionIds, startsExchange, summarizeChanges, threadRuns, type ProposalState } from '../assistant'
import { AutofixBanner } from './AutofixReport'
import { Disclosure } from './Disclosure'
import { ActionButton, ChoiceButtons } from './Choices'
import { summarize } from '../selection'
import { useStore } from '../store'
import { attachmentSummary } from '../documents'
import { providerOf, providerOptions, settingsSummary, shortModel, shownChoice } from '../providers'
import { citationSummary } from '../citations'
import { CitationItem } from './Citation'
import type { AgentRun, Proposal, ProposalCitations, ProviderHealth, RunChanges } from '../types'

const FIELD_LABELS: Record<string, string> = {
  label: 'Name', equipment: 'Equipment', point_kind: 'Kind', point_type: 'Point type', quantity_kind: 'Measurement', unit: 'Unit',
  sensor_type: 'Sensor type', medium: 'Medium', substance: 'Substance', type: 'Type', process: 'Process',
  contained_in: 'Part of', from_equipment: 'From', to_equipment: 'Connected to',
  direction: 'Direction', connection: 'Connection', paired_with: 'Paired with', maps_to: 'Maps to',
  from_point: 'From point', to_point: 'To point', part_of: 'Part of', location: 'Location',
  subject: 'From', relation: 'Relation', object: 'To',
}
const fieldLabel = (field: string) => FIELD_LABELS[field] ?? field.replace(/_/g, ' ')
const settingsNote = (settings: Parameters<typeof settingsSummary>[0]) => {
  const summary = settingsSummary(settings)
  return summary ? ` (${summary})` : ''
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
  const cleared = useStore((s) => s.assistantCleared)
  const clearChat = useStore((s) => s.clearChat)
  // Only whether there is a draft: subscribing to the text would redraw the whole thread per keystroke.
  const hasDraft = useStore((s) => s.assistantDraft.length > 0)
  const sending = useStore((s) => s.assistantSending)
  const busy = useStore((s) => s.busy)
  const thread = useMemo(() => threadRuns(runs, cleared.runs), [runs, cleared.runs])
  const running = Object.values(runs).some((r) => r.kind !== 'autofix' && isActive(r))
  const last = thread[thread.length - 1]
  const orphan = proposal && !owningRun(thread, proposal.id) ? proposal : null
  const threadRef = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)

  // Follow new messages and progress while the reader is at the bottom of the thread.
  useLayoutEffect(() => {
    const el = threadRef.current
    if (el && pinned.current) el.scrollTop = el.scrollHeight
  }, [thread.length, last?.status, last?.progress.length, proposal?.id, proposal?.status, sending])

  return (
    <aside className="assistant">
      <div className="assistant-head">
        <h2>Assistant</h2><ModelStatus />
        <button disabled={running || busy || (!thread.length && !proposal && !hasDraft)} onClick={clearChat}
          title={running ? 'Wait for the assistant to finish before clearing the chat' : 'Clear this conversation and start a new chat'}>
          Clear chat
        </button>
      </div>
      <AutofixBanner />
      <div className="thread" ref={threadRef} onScroll={(e) => {
        const el = e.currentTarget
        pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
      }}>
        {!thread.length && !orphan && !sending && <Starters />}
        {thread.map((run, i) => <Exchange key={run.id} run={run} divider={i > 0 && startsExchange(run)}
          isLast={run === last && !newRequest} />)}
        {orphan && <ProposalPreview proposal={orphan} running={!!last && isActive(last)} />}
        {sending && <div className="msg user sending">
          <div className="msg-author">You <span className="spinner small" /><span className="muted small">sending…</span></div>
          <div className="msg-text">{sending}</div>
        </div>}
      </div>
      <Composer />
    </aside>
  )
}

/** One request and its reply; redrawn only when that run changes, not on every update elsewhere. */
const Exchange = memo(function Exchange({ run, divider, isLast }: { run: AgentRun; divider: boolean; isLast: boolean }) {
  return <div className="exchange">
    {divider && <div className="thread-divider"><span>New request</span></div>}
    <UserMessage run={run} />
    <AssistantMessage run={run} isLast={isLast} />
  </div>
})

/** When the model changed since the reply a message continues (edits, undo, other changes; not
 *  applying that reply's own proposal), the person chooses: continue its conversation, told what
 *  changed, or start a fresh one with the current model. */
function ModelChangedChoice({ runId }: { runId: string }) {
  const projectId = useStore((s) => s.projectId)
  const head = useStore((s) => s.model?.head)
  const fresh = useStore((s) => s.assistantFresh)
  const setFresh = useStore((s) => s.setAssistantFresh)
  const [changes, setChanges] = useState<RunChanges | null>(null)
  useEffect(() => {
    if (!projectId) return
    let live = true
    api.runChanges(projectId, runId).then((c) => { if (live) setChanges(c) }).catch(() => { if (live) setChanges(null) })
    return () => { live = false }
  }, [projectId, runId, head])
  if (!changes?.changed || !changes.continuable) return null
  return <div className="composer-changed">
    <span>{changes.summary}</span>
    <span className="choice-pair" role="radiogroup" aria-label="How to continue">
      <button role="radio" aria-checked={!fresh} className={fresh ? '' : 'active'} onClick={() => setFresh(false)}
        title="Continue the conversation; the assistant is told what changed">Keep going</button>
      <button role="radio" aria-checked={fresh} className={fresh ? 'active' : ''} onClick={() => setFresh(true)}
        title="A new conversation with the current model; the earlier chat goes along as text">Start fresh</button>
    </span>
  </div>
}

function Composer() {
  const selection = useStore((s) => s.selection)
  const rows = useStore((s) => s.rows)
  const clearSelection = useStore((s) => s.clearSelection)
  const sendMessage = useStore((s) => s.sendMessage)
  const runs = useStore((s) => s.runs)
  const cleared = useStore((s) => s.assistantCleared)
  const proposal = useStore((s) => s.proposal)
  const text = useStore((s) => s.assistantDraft)
  const setText = useStore((s) => s.setAssistantDraft)
  const draftVersion = useStore((s) => s.assistantDraftVersion)
  const newRequest = useStore((s) => s.assistantNewRequest)
  const setNewRequest = useStore((s) => s.setAssistantNewRequest)
  const [sending, setSending] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const viewing = useStore((s) => s.viewing)
  const issues = useStore((s) => s.model?.issues)
  const startAutofix = useStore((s) => s.startAutofix)
  const autofixing = useStore((s) => !!s.autofix)

  useEffect(() => {
    if (!draftVersion) return
    const textarea = textareaRef.current
    if (textarea) {
      textarea.focus()
      textarea.setSelectionRange(textarea.value.length, textarea.value.length)
    }
  }, [draftVersion])

  const thread = useMemo(() => threadRuns(runs, cleared.runs), [runs, cleared.runs])
  const last = thread[thread.length - 1]
  const running = !!last && isActive(last)
  const next = continuation(thread, proposal, newRequest)
  const canContinue = continuation(thread, proposal, false) !== null
  const selectionVersion = useStore((s) => s.selectionVersion)
  const requestVersion = useStore((s) => (next?.runId ? s.runSelectionVersion[next.runId] : undefined) ?? 0)
  const noScope = useStore((s) => s.assistantNoScope)
  const dropScope = useStore((s) => s.dropConversationScope)
  const { selection: about, source } = effectiveSelection(next, selection, thread, proposal, selectionVersion > requestVersion, noScope)
  const summary = summarize(about, rows)
  const chosen = [...about.entity_ids, ...about.relationship_ids].map((id) => rows.get(id)).filter(Boolean)
  const fixable = source === 'current' && !autofixing && !viewing ? issuesOnSelection(issues ?? [], selection) : []

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
        ? 'This message stays about what the conversation was about, since you have not selected anything since that request. Select something to change it.'
        : source === 'proposal' ? 'A reply revises the proposal, so it keeps the proposal’s selection.' : undefined}>
        <span className="muted">{source === 'current' ? 'Selected:'
          : source === 'conversation' ? 'About (from the conversation):' : 'About (the proposal’s selection):'}</span>
        {chosen.length ? <>
          {chosen.slice(0, 6).map((r) => <span key={r!.id} className={`chip ${r!.kind}`}>{r!.label}</span>)}
          {chosen.length > 6 && <span className="muted">+{chosen.length - 6} more</span>}
          {source === 'current' && <button className="link" onClick={clearSelection}>Clear</button>}
          {source === 'conversation' && <button className="link" onClick={dropScope}>Clear</button>}
          {fixable.length > 0 && <button className="link" disabled={running}
            title="The assistant fixes these issues group by group; fixes that pass every check are applied (and can be undone), the rest wait for you"
            onClick={() => void startAutofix(fixable)}>· Auto-fix {fixable.length} issue{fixable.length === 1 ? '' : 's'}</button>}
        </> : <span className="muted">{summary}</span>}
      </div>
      {selection.source_regions.length > 0 && <div className="composer-selection">
        <span className="muted" title="Sent with this message only; attach again to use them in a later message">
          Attached to this message: {attachmentSummary(selection.source_regions)}</span>
        <button className="link" onClick={() => useStore.getState().setSelection({ ...selection, source_regions: [] })}>Clear</button>
      </div>}
      {next?.runId && <ModelChangedChoice runId={next.runId} />}
      {canContinue && <div className={`composer-mode mode-${next ? next.kind : 'new'}`}>
        {next ? <>
          <span>↩ {CONTINUE_LABEL[next.kind]}</span>
          <button className="link" onClick={() => setNewRequest(true)}>Start a new request</button>
        </> : <>
          <span title="The assistant won’t see the earlier conversation">New request</span>
          <button className="link" onClick={() => setNewRequest(false)}>Continue instead</button>
        </>}
      </div>}
      <textarea
        ref={textareaRef}
        disabled={!!viewing}
        placeholder={viewing ? `Viewing ${viewing} (read-only). Go back to the current model to ask for changes.`
          : next?.kind === 'proposal' ? 'Tell the assistant what to revise, e.g. “A2 is also a VAV.”'
          : next?.kind === 'questions' ? 'Answer the assistant’s questions…'
            : chosen.length ? 'Describe a change, e.g. “These belong to RO-1.”'
              : 'Select items in a table or the graph, then describe a change.'}
        value={text} onChange={(e) => setText(e.target.value)} rows={3}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send() }
        }}
      />
      <div className="assist-actions">
        <span className="spacer" />
        <button className="primary" disabled={!!viewing || !text.trim() || running || sending} onClick={() => void send()}
          title="Enter to send · Shift+Enter for a new line">
          {next?.kind === 'proposal' ? 'Send reply' : 'Send'}
        </button>
      </div>
    </div>
  )
}

/** The model endpoint the assistant uses, and whether it can be reached right now. */
function ModelStatus() {
  const status = useStore((s) => s.status)
  const provider = useStore((s) => s.provider)
  const setProvider = useStore((s) => s.setProvider)
  const listed = useStore((s) => s.providerModels)
  const setProviderModels = useStore((s) => s.setProviderModels)
  const setModelVision = useStore((s) => s.setModelVision)
  const [health, setHealth] = useState<ProviderHealth | null>(null)
  const [checkVersion, setCheckVersion] = useState(0)
  const providers = useMemo(() => status?.providers ?? [], [status])
  const current = provider ? providerOf(provider, providers) : undefined

  useEffect(() => {
    if (!provider) return
    let alive = true
    const check = () => api.providerHealth(provider).then((h) => {
      if (!alive) return
      setHealth(h)
      if (current?.lists_models && h.models) setProviderModels(current.name, h.models)
      if (h.ok && h.supports_images !== undefined) setModelVision(provider, h.supports_images)
    }).catch((e) => alive && setHealth({ ok: false, detail: `Could not check the model endpoint: ${(e as Error).message}` }))
    void check()
    const t = setInterval(check, 15000)
    return () => { alive = false; clearInterval(t) }
  }, [provider, checkVersion, current, setProviderModels, setModelVision])

  // The other servers' lists, so their models can be chosen before switching to them.
  useEffect(() => {
    let alive = true
    for (const p of providers) {
      if (p.lists_models && p.name !== current?.name) {
        void api.providerHealth(p.name).then((h) => { if (alive && h.models) setProviderModels(p.name, h.models) }).catch(() => {})
      }
    }
    return () => { alive = false }
  }, [providers, current, setProviderModels])

  const options = providerOptions(providers, listed)
  const shown = shownChoice(options, provider, providers, listed)
  const state = !health ? 'checking' : health.ok ? 'ok' : 'bad'
  return <>
    <div className="model-status">
      <span className={`status-pill ${state}`} title={health?.ok
        ? `Connected to ${current?.base_url ?? provider} · ${shortModel(health.model ?? current?.model ?? '')}`
        : health?.detail ?? 'Checking the model endpoint…'}>
        <span className="dot" />{state === 'checking' ? 'Checking…' : state === 'ok' ? '' : 'Unavailable'}
      </span>
      <select value={shown.value} title="Model" onChange={(e) => {
        setHealth(null)
        setProvider(e.target.value)
      }}>
        {options.map((o) => <option key={o.value} value={o.value} disabled={o.disabled}>{o.label}</option>)}
        {shown.missing && <option value={shown.missing.value}>{shown.missing.label}</option>}
      </select>
    </div>
    {state === 'bad' && <div className="model-status-detail">
      <span>{health!.detail} Requests can’t run until it is reachable.</span>
      <button className="link" onClick={() => { setHealth(null); setCheckVersion((v) => v + 1) }}>Check again</button>
    </div>}
  </>
}

const STARTER_ISSUES = 10
const AUTOFIX_HINT = 'Fixes that pass every check are applied (and can be undone); the rest wait for you'

/** One-click ways to start a conversation; each fills the message box for review first. */
function Starters() {
  const issues = useStore((s) => s.model?.issues)
  const selection = useStore((s) => s.selection)
  const addIssuesToPrompt = useStore((s) => s.addIssuesToPrompt)
  const appendAssistantContext = useStore((s) => s.appendAssistantContext)
  const startAutofix = useStore((s) => s.startAutofix)
  const autofixing = useStore((s) => !!s.autofix)
  const violations = (issues ?? []).filter((i) => i.resolution_state === 'open' && i.severity === 'violation')
  const selected = selection.entity_ids.length + selection.relationship_ids.length > 0
  const onSelection = selected && !autofixing ? issuesOnSelection(issues ?? [], selection) : []
  const starters = [
    onSelection.length > 0 && {
      label: `Auto-fix the ${onSelection.length} issue${onSelection.length === 1 ? '' : 's'} on the selection`,
      hint: AUTOFIX_HINT,
      run: () => void startAutofix(onSelection),
    },
    violations.length > 1 && !autofixing && {
      label: `Auto-fix the ${violations.length} open violations`,
      hint: AUTOFIX_HINT,
      run: () => void startAutofix(autofixCandidates(issues ?? [], new Set())),
    },
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
    <div className="starters">
      <span className="muted small">Start with</span>
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
      {hidden > 0 && <li className="muted"><button className="link" onClick={() => setShowAll(true)}>
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
      {!live && <li className="run-meta muted">{run.provider} · <span title={JSON.stringify(run.model_settings ?? {}, null, 1)}>
        {run.model}{settingsNote(run.model_settings)}</span> · skill {run.skill_version}
        {run.outcome.input_tokens ? <span title={callBreakdown(run.outcome.calls)}>
          {` · ${run.outcome.estimated ? '~' : ''}${run.outcome.input_tokens}+${run.outcome.output_tokens} tokens`}
          {run.outcome.cached_tokens ? ` (${run.outcome.cached_tokens} cached)` : ''}</span> : ''}</li>}
    </ol>
    {slowModel && <div className="muted small step-note">Still working; local models can take a minute or more per step.</div>}
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
    {long && <button className="link" onClick={() => setOpen(!open)}>{open ? 'Show less' : 'Show more'}</button>}
  </>
}

function AssistantMessage({ run, isLast }: { run: AgentRun; isLast: boolean }) {
  const cancelRun = useStore((s) => s.cancelRun)
  const proposal = useStore((s) => s.proposal)
  const states = useStore((s) => s.proposalStates)
  const focusAssistant = useStore((s) => s.focusAssistant)
  const active = isActive(run)
  const now = useNow(active)
  const elapsed = (active ? now : Date.parse(run.finished_at ?? run.created_at)) - Date.parse(run.created_at)
  const proposalId = run.status === 'succeeded' ? run.outcome.proposal_id : null
  const choices = run.status === 'succeeded' && !proposalId ? run.outcome.choices ?? [] : []
  // The proposal on screen belongs here if this run proposed it or offered it as an option.
  const current = proposal && (proposal.id === proposalId || (choices.length > 0 && runOptionIds(run).includes(proposal.id)))
    ? proposal : null
  const questions = !proposalId ? run.outcome.questions ?? [] : []

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
        {!proposalId && run.outcome.explanation && <div className="msg-text">{run.outcome.explanation}</div>}
        {!run.outcome.explanation && !proposalId && !questions.length && !choices.length && <div className="muted">No change proposed.</div>}
        {run.outcome.dismissed_proposal_id && <div className="thread-event">The earlier proposal is no longer needed and was closed.</div>}
      </>}
      {questions.length > 0 && <div className="needs-input">
        <div className="needs-input-head"><span className="needs-input-icon">?</span> Needs your input</div>
        <ul>{questions.map((q, i) => <li key={i}>{q}</li>)}</ul>
        {isLast && <button className="primary" onClick={focusAssistant}>Answer below</button>}
      </div>}
      {choices.length > 0 && <div className="needs-input choices">
        <div className="needs-input-head"><span className="needs-input-icon">?</span> Pick an option
          <span className="muted small"> · each opens as a change to review{isLast ? ', or answer below' : ''}</span></div>
        {choices.map((c, k) => <ChoiceButtons key={k} choice={c} review />)}
      </div>}
      {current
        ? <ProposalPreview proposal={current} running={false} />
        : proposalId && <div className="proposal-ref">Proposed change · {states[proposalId] === undefined && isLast
          ? <><span className="spinner small" /> loading…</> : proposalStateText(states[proposalId])}</div>}
      {!active && <Disclosure className="run-details"
        summary={`Run details · ${run.progress.length} step${run.progress.length === 1 ? '' : 's'} · ${formatDuration(elapsed)}`}>
        {() => <>
          {proposalId && run.outcome.explanation && <LongText text={run.outcome.explanation} />}
          {run.parent_run_id && <div className="muted">{run.continued ? 'Continued the earlier conversation.'
            : 'Started a fresh conversation with the current model.'}</div>}
          <RunSteps run={run} live={false} />
        </>}
      </Disclosure>}
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

const capitalize = (text: string) => text.charAt(0).toUpperCase() + text.slice(1)

function fmt(v: unknown) {
  if (v === null || v === undefined || v === '') return <span className="muted">—</span>
  return String(v)
}

const FACE_LINES = 8
const CITED_CHANGES_SHOWN = 50

/** What each change cites, fetched when the section opens: a build can cite many sources. */
function ProposalSources({ proposal }: { proposal: Proposal }) {
  const projectId = useStore((s) => s.projectId)!
  const [data, setData] = useState<ProposalCitations | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [all, setAll] = useState(false)
  useEffect(() => {
    let active = true
    api.proposalCitations(projectId, proposal.id).then((d) => { if (active) setData(d) })
      .catch((e) => { if (active) setError((e as Error).message) })
    return () => { active = false }
  }, [projectId, proposal.id])
  if (error) return <p className="error-text">{error}</p>
  if (!data) return <p className="muted">Loading evidence…</p>
  const items = (ids: string[]) => ids.flatMap((id) => data.observations[id] ? [data.observations[id]] : [])
    .map((c) => <CitationItem key={c.id} citation={c} />)
  const changes = proposal.changes.filter((c) => c.change !== 'deleted')
  return <div className="proposal-sources">
    {(all ? changes : changes.slice(0, CITED_CHANGES_SHOWN)).map((change) => <section key={change.entity_id}>
      <h5><span className={`change-kind ${change.change}`}>{change.change}</span> {change.label}</h5>
      {data.changes[change.entity_id]?.length ? items(data.changes[change.entity_id])
        : <p className="warn-text">No source linked.</p>}
    </section>)}
    {!all && changes.length > CITED_CHANGES_SHOWN && <button className="link" onClick={() => setAll(true)}>
      Show all {changes.length} changes</button>}
    {data.other.length > 0 && <section><h5>Linked to relations</h5>{items(data.other)}</section>}
    {data.read.length > 0 && <section><h5>Read, not linked</h5>{items(data.read)}</section>}
  </div>
}

function evidenceSummary({ cited, total, read }: { cited: number; total: number; read: number }) {
  const parts = total ? [`${cited} of ${total} change${total === 1 ? '' : 's'} link${total === 1 ? 's' : ''} to a source`] : []
  if (read) parts.push(`${read} read, not linked`)
  return `Evidence · ${parts.join(' · ')}`
}

function ProposalPreview({ proposal, running: runningProp }: {
  proposal: Proposal; running: boolean
}) {
  const projectId = useStore((s) => s.projectId)
  const autofixRunning = useStore((s) => Object.values(s.runs).some((r) => r.kind === 'autofix' && isActive(r)))
  const otherRunning = useStore((s) => Object.values(s.runs).some((r) => r.kind !== 'autofix' && isActive(r)))
  const running = runningProp || autofixRunning || otherRunning
  const waitTitle = autofixRunning ? 'Wait for auto-fix to finish (or stop it) before changing the model'
    : running ? 'Wait for the assistant to finish' : undefined
  const applyProposal = useStore((s) => s.applyProposal)
  const dismissProposal = useStore((s) => s.dismissProposal)
  const regenerate = useStore((s) => s.regenerate)
  const click = useStore((s) => s.click)
  const inspect = useStore((s) => s.inspect)
  const busy = useStore((s) => s.busy)
  const head = useStore((s) => s.model?.head)
  const viewing = useStore((s) => s.viewing)
  const [allLines, setAllLines] = useState(false)
  const [expanded, setExpanded] = useState(false)  // an applied proposal folds to one line
  const v = proposal.validation
  const lines = useMemo(() => summarizeChanges(proposal.changes, fieldLabel), [proposal])
  const outside = lines.filter((l) => l.outside).length
  const dismissals = proposal.issue_dismissals ?? []
  const leftOut = proposal.left_out ?? []
  const one = leftOut.length === 1
  const replyToProposal = useStore((s) => s.replyToProposal)
  const evidence = useMemo(() => citationSummary(proposal), [proposal])
  const fixes = [...new Set([...(v?.resolved ?? []), ...(proposal.gate?.fixed ?? [])])]
  const introduced = [...new Set([...(v?.introduced ?? []), ...(proposal.gate?.introduced ?? [])])]
  const show = (id: string, deleted: boolean, ctrl: boolean) => {
    if (!deleted) click({ id }, { ctrl, shift: false }, [])
    inspect(id)
  }
  const counts = { created: 0, updated: 0, deleted: 0 }
  for (const l of lines) counts[l.change]++
  const headline = [counts.created && `${counts.created} added`, counts.updated && `${counts.updated} changed`,
    counts.deleted && `${counts.deleted} removed`].filter(Boolean).join(' · ')

  if (proposal.status === 'applied' && !expanded) return (
    <div className="proposal status-applied collapsed">
      <span className="ok-text">✓ {proposal.operations.length ? `Applied as ${proposal.applied_revision}` : `Dismissed ${dismissals.length} issue(s)`}</span>
      {headline && <span className="muted"> · {headline}</span>}
      {fixes.length > 0 && <span className="muted"> · fixes {fixes.length} issue{fixes.length === 1 ? '' : 's'}</span>}
      <button className="link" onClick={() => setExpanded(true)}>Show</button>
    </div>
  )

  // Issues in one line: what it fixes or dismisses, and whether it adds any. Bad news stays open.
  const checked = !!(v || proposal.gate)
  const plural = (n: number, what: string) => `${n} ${what}${n === 1 ? '' : 's'}`
  const issueLine = [fixes.length > 0 && `fixes ${plural(fixes.length, 'issue')}`,
    dismissals.length > 0 && `dismisses ${plural(dismissals.length, 'issue')}`,
    checked && !introduced.length && 'no new issues'].filter(Boolean).join(' · ')
  const sources = proposal.build_summary?.sources ?? []
  const fieldChanges = proposal.changes.some((c) => c.fields.length > 2)

  return (
    <div className={`proposal status-${proposal.status}`}>
      <div className="proposal-head">
        <h3>Proposed change</h3>
        {headline && <span className="muted">{headline}</span>}
        {outside > 0 && <span className="warn-text">{outside} outside your selection</span>}
        <span className="spacer" />
        {proposal.status === 'applied' && <button className="link" onClick={() => setExpanded(false)}>Hide</button>}
      </div>

      {lines.length > 0 && <div className="proposal-changes">
        <ul>{(allLines ? lines : lines.slice(0, FACE_LINES)).map((l) => (
          <li key={l.id} className={`change-line ${l.change}`} title="Select and inspect"
            onClick={(e) => show(l.id, l.change === 'deleted', e.ctrlKey || e.metaKey)}>
            <span className="change-mark">{l.change === 'created' ? 'Add' : l.change === 'deleted' ? 'Remove' : 'Change'}</span>
            <span className="change-label">{l.label}</span>
            <span className="change-detail muted">{l.detail}</span>
            {l.outside && <span className="badge" title="Outside your selection">outside</span>}
            {l.overridesEdit && <span className="badge warn" title="A person set one of these values earlier">overrides edit</span>}
          </li>))}</ul>
        {lines.length > FACE_LINES && <button className="link" onClick={() => setAllLines(!allLines)}>
          {allLines ? 'Show fewer' : `Show ${lines.length - FACE_LINES} more`}</button>}
      </div>}
      {!lines.length && !dismissals.length && <p className="muted">No model changes were needed.</p>}

      <div className="proposal-fixes">
        {fixes.length > 0 || dismissals.length > 0
          ? <Disclosure summary={capitalize(issueLine)} summaryClassName={introduced.length ? undefined : 'resolved'}>
            {fixes.length > 0 && <ul>{fixes.map((f, i) => <li key={i}>{f}</li>)}</ul>}
            {dismissals.length > 0 && <ul>{dismissals.map((d) => <li key={d.id}>{d.explanation}
              <div className="muted">Dismissed: {d.reason}</div></li>)}</ul>}
          </Disclosure>
          : issueLine ? <div className="ok-text">{capitalize(issueLine)}</div>
          : !checked && <div className="muted">Issue checks unavailable</div>}
        {introduced.length > 0 && <>
          <div className="fixes-head introduced">Adds {plural(introduced.length, 'new issue')}</div>
          <ul className="introduced">{introduced.map((f, i) => <li key={i}>{f}</li>)}</ul>
        </>}
      </div>

      {proposal.questions.length > 0 && <div className="proposal-notes">
        {proposal.questions.map((q, i) => <p key={`q${i}`} className="question">? {q}</p>)}
      </div>}

      {leftOut.length > 0 && <div className="proposal-left-out">
        <p className="warn-text"><strong>{leftOut.length} operation{one ? '' : 's'} left out</strong>
          {' '}— the assistant could not make {one ? 'it' : 'them'} valid, so {one ? 'it is' : 'they are'} not part of this proposal.</p>
        <ul>{leftOut.map((e, i) => <li key={i}><code>{leftOutSummary(e.operation)}</code>: {e.problems.join('; ')}</li>)}</ul>
        {proposal.status === 'pending' && <button className="link" disabled={running || busy} title={waitTitle}
          onClick={() => void replyToProposal('Try the left-out operations again.', proposal.agent_run_id ?? undefined, true)}>
          Try {one ? 'it' : 'these'} again</button>}
      </div>}

      {/* Above Details, not in it: how much of this is backed by a source is part of deciding. */}
      {(evidence.cited > 0 || evidence.read > 0) && <Disclosure className="proposal-details" summary={evidenceSummary(evidence)}>
        {() => <ProposalSources proposal={proposal} />}
      </Disclosure>}

      {/* Everything for a closer look, in one place: sources, notes, field changes, RDF. */}
      <Disclosure className="proposal-details" summary={['Details', proposal.notes.length > 0 && plural(proposal.notes.length, 'note'),
        `+${proposal.diff.added.length} / −${proposal.diff.removed.length} triples`].filter(Boolean).join(' · ')}>
        {() => <>
          {sources.length > 0 && <section>
            <h4>Sources</h4>
            <ul>{sources.map((source) => <li key={source.id}>
              <a href={`${api.sourceFileUrl(projectId!, source.id)}${source.pages?.length ? `#page=${source.pages[0]}` : ''}`}
                target="_blank" rel="noreferrer">{source.filename}</a>
              <span className="muted"> · {source.role === 'reference' ? 'Supporting evidence' : 'Build input'}
                {source.pages?.length ? ` · pages ${source.pages.join(', ')}` : ''}</span>
            </li>)}</ul>
          </section>}
          {proposal.notes.length > 0 && <section className="proposal-notes">
            <h4>Notes</h4>
            {proposal.notes.map((n, i) => <p key={i} className="warn-text">{n}</p>)}
          </section>}
          {fieldChanges && <section>
            <h4>Field changes</h4>
            {proposal.changes.map((change) => {
              const fields = change.fields.length ? change.fields : [{ field: change.change, before: null, after: null }]
              return <table key={change.entity_id} className="changes">
                <thead><tr><th colSpan={3}><span className={`change-kind ${change.change}`}>{change.change}</span> {change.label}</th></tr></thead>
                <tbody>{fields.map((field, index) => (
                  <tr key={`${field.field}-${index}`} onClick={(e) => show(change.entity_id, change.change === 'deleted', e.ctrlKey || e.metaKey)}>
                    <td>{fieldLabel(field.field)}
                      {change.overrides_locked.includes(field.field) && <span className="badge warn" title="A person set this value earlier">overrides earlier edit</span>}
                    </td>
                    <td className="before">{fmt(field.before)}</td>
                    <td className="after">{fmt(field.after)}</td>
                  </tr>
                ))}</tbody>
              </table>
            })}
          </section>}
          <section>
            <h4>RDF</h4>
            <pre className="code diff">{[
              ...proposal.diff.removed.map((t) => `- ${t}`),
              ...proposal.diff.added.map((t) => `+ ${t}`),
            ].join('\n') || 'No RDF changes.'}</pre>
          </section>
        </>}
      </Disclosure>

      <div className="proposal-actions">
        {(proposal.status === 'pending' || proposal.status === 'stale') && <>
          {proposal.status === 'stale' && <span className="warn-text">
            The model is now at {head}. Applying replays these operations and may overwrite newer values.
          </span>}
          <ActionButton className="primary" disabled={!!viewing || busy || running || (proposal.operations.length === 0 && !dismissals.length)}
            title={viewing ? 'Go back to the current model to apply it' : waitTitle}
            onClick={applyProposal}>{proposal.operations.length ? 'Apply change'
              : `Dismiss ${dismissals.length} issue(s)`}</ActionButton>
          {proposal.status === 'stale' && <ActionButton disabled={!!viewing || busy || running} title={waitTitle}
            onClick={regenerate}>Refresh on latest</ActionButton>}
          <ActionButton disabled={busy || running} title={waitTitle} onClick={dismissProposal}>Discard</ActionButton>
        </>}
        {proposal.status === 'applied' && <span className="ok-text">{proposal.operations.length
          ? `Applied as ${proposal.applied_revision}.` : `Dismissed ${dismissals.length} issue(s).`}</span>}
      </div>
    </div>
  )
}
