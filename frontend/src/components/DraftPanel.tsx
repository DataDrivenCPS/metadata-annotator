/** Draft mode's toolbar and editor: changes drawn on the graph stay in the browser until they are
 * checked (validation only) or saved together as one edit. See draft.ts for the draft itself. */
import { useEffect, useRef, useState } from 'react'
import { isActive } from '../assistant'
import {
  assignPorts, draftCount, isDraftId, portOptions, removeItem, setField, setPort,
  type Draft, type DraftField, type DraftProblem, type FieldValue,
} from '../draft'
import { useStore } from '../store'
import type { ConnectionRow, EntityRef, EquipmentRow, ModelResponse, ReviewIssue, Term, TermRef } from '../types'
import { shortIri, TermPicker } from './TermPicker'

/** dataTransfer type of the palette's "+ Equipment" chip. */
export const EQUIPMENT_DRAG = 'application/x-workbench-new-equipment'

export function PencilIcon() {
  return <svg width="13" height="13" viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round">
    <path d="M11.3 2.2l2.5 2.5-8.6 8.6-3.2.7.7-3.2z" /><path d="M9.8 3.7l2.5 2.5" />
  </svg>
}

interface BarProps {
  draft: Draft
  problems: DraftProblem[]
  labelOf: (id: string) => string
  onFocus: (id: string) => void
  onAddEquipment: () => void
  confirming: boolean
  setConfirming: (v: boolean) => void
}

export function DraftBar({ draft, problems, labelOf, onFocus, onAddEquipment, confirming, setConfirming }: BarProps) {
  const busy = useStore((s) => s.busy)
  const check = useStore((s) => s.draftCheck)
  const checkDraft = useStore((s) => s.checkDraft)
  const saveDraft = useStore((s) => s.saveDraft)
  const discardDraft = useStore((s) => s.discardDraft)
  const running = useStore((s) => Object.values(s.runs).some(isActive))
  const [showIssues, setShowIssues] = useState(false)
  const n = draftCount(draft)
  const ready = n > 0 && problems.length === 0 && !busy
  const result = check && check.draft === draft ? check : null  // a check of an earlier draft says nothing
  const placeholderOf = new Map(Object.entries(result?.ids ?? {}).map(([k, v]) => [v, k]))
  const focusIssue = (i: ReviewIssue) => {
    const id = i.affected_ids.map((a) => placeholderOf.get(a) ?? a)[0]
    if (id) onFocus(id)
  }

  return (
    <div className="draft-bar" role="toolbar" aria-label="Draft changes">
      <div className="draft-row">
        <strong>Draft</strong>
        <span className="muted">{n ? `${n} change${n === 1 ? '' : 's'}, not saved` : 'nothing drafted yet'}</span>
        <button className="draft-chip" draggable
          onDragStart={(e) => { e.dataTransfer.setData(EQUIPMENT_DRAG, 'equipment'); e.dataTransfer.effectAllowed = 'copy' }}
          onClick={onAddEquipment} title="Drag onto the graph to place new equipment there (or click to add it in the middle)">
          + Equipment
        </button>
        <span className="spacer" />
        <button disabled={!ready || result?.status === 'checking'} onClick={() => void checkDraft()}
          title="Validate the draft without saving it (no assistant call)">Check</button>
        <button className="primary" disabled={!ready} onClick={() => void saveDraft(false)}
          title="Save every drafted change as one revision (undo reverts all of it)">Save</button>
        <button disabled={!ready || running} onClick={() => void saveDraft(true)}
          title={running ? 'Wait for the assistant to finish'
            : 'Save, then let auto-fix work through the issues the draft introduced (one assistant run; it does not change what you set)'}>
          Save &amp; fix issues
        </button>
        <button className="link" disabled={busy} onClick={() => (n ? setConfirming(true) : discardDraft())}>Discard</button>
      </div>
      {confirming && <div className="draft-row draft-confirm">
        Discard {n} drafted change{n === 1 ? '' : 's'}?
        <button onClick={() => { setConfirming(false); discardDraft() }}>Discard</button>
        <button className="link" onClick={() => setConfirming(false)}>Keep drafting</button>
      </div>}
      {n === 0 && <div className="draft-row muted small">
        Drag from a node’s right edge onto another node to connect them · drag <em>+ Equipment</em> onto the graph · click anything to edit it
      </div>}
      {problems.length > 0 && <ul className="draft-problems">
        {problems.map((p, i) => <li key={i}>
          <button className="link" onClick={() => onFocus(p.id)}>{labelOf(p.id)}</button> {p.message}
        </li>)}
      </ul>}
      {result && problems.length === 0 && <div className="draft-row small">
        {result.status === 'checking' && <><span className="spinner" /> Checking…</>}
        {result.status === 'error' && <span className="error-text">Check failed: {result.error}</span>}
        {result.status === 'done' && <>
          <span className={result.introduced.length ? 'warn-text' : 'ok-text'}>
            {result.introduced.length ? `Would introduce ${result.introduced.length} issue${result.introduced.length === 1 ? '' : 's'}` : 'Introduces no issues'}
          </span>
          {result.resolved.length > 0 && <span className="ok-text">· resolves {result.resolved.length}</span>}
          {result.introduced.length > 0 && <button className="link" onClick={() => setShowIssues(!showIssues)}>{showIssues ? 'hide' : 'show'}</button>}
          {result.notes.length > 0 && <span className="muted">· {result.notes.join('; ')}</span>}
        </>}
      </div>}
      {result?.status === 'done' && showIssues && result.introduced.length > 0 && <ul className="draft-issues">
        {result.introduced.map((i) => <li key={i.id} className={`sev-${i.severity}`}>
          <button className="link" onClick={() => focusIssue(i)}>{i.explanation}</button>
        </li>)}
      </ul>}
    </div>
  )
}

interface PanelProps {
  id: string
  model: ModelResponse
  draft: Draft
  equipment: EquipmentRow[]
  connections: ConnectionRow[]
  problems: DraftProblem[]
  onClose: () => void
}

type AnyRow = {
  label: string; type: TermRef | null; process?: TermRef | null; medium?: TermRef | null
  from_equipment?: EntityRef | null; to_equipment?: EntityRef | null
}

const pickedValue = (iri: string | null, t?: Term): FieldValue => (iri ? { value: iri, label: t?.label ?? shortIri(iri) } : { value: null, label: '' })

/** Edit one drafted or existing object; every change stays in the draft. Mount with ``key={id}``. */
export function DraftPanel({ id, model, draft, equipment, connections, problems, onClose }: PanelProps) {
  const setGraphDraft = useStore((s) => s.setGraphDraft)
  const s223 = model.info.family === 's223'
  const hasProcess = model.info.profile === 'watr'
  const eq = equipment.find((e) => e.id === id)
  const cx = eq ? undefined : connections.find((c) => c.id === id)
  const newCx = draft.connections.find((c) => c.key === id)
  const isNew = isDraftId(id)
  const [picking, setPicking] = useState<DraftField | null>(newCx && s223 && !newCx.medium ? 'medium' : null)
  const name = useRef<HTMLInputElement>(null)
  // New equipment opens with its placeholder name selected, so typing replaces it (once, not per keystroke).
  const freshEquipment = isNew && !!eq
  useEffect(() => { if (freshEquipment) name.current?.select() }, [freshEquipment])
  if (!eq && !cx) return null

  const set = (field: DraftField, v: FieldValue) => setGraphDraft(setField(model, draft, id, field, v))
  const labelOf = (eid: string) => equipment.find((e) => e.id === eid)?.label ?? eid
  const orig: AnyRow | undefined = model.view.equipment.find((e) => e.id === id) ?? model.view.connections.find((c) => c.id === id)
  /** What the model has for an edited field. */
  const was = (field: DraftField, value: (r: AnyRow) => string | undefined) => {
    if (!orig || !draft.edits[id]?.[field]) return null
    return <div className="draft-was">was {value(orig) || '—'}</div>
  }
  const term = (field: DraftField, kind: string, value: string | undefined, placeholder: string, allowClear: boolean) => (
    picking === field
      ? <TermPicker kind={kind} allowClear={allowClear} onCancel={() => setPicking(null)}
          onPick={(iri, t) => { setPicking(null); set(field, pickedValue(iri, t)) }} />
      : <>{value ?? <span className="muted">{placeholder}</span>} <button className="link" onClick={() => setPicking(field)}>change</button></>
  )
  const mine = problems.filter((p) => p.id === id)
  // A new connection that joins existing connection points carries their medium unless one is chosen.
  const ends = newCx ? assignPorts(model, draft).get(id) : undefined
  const portMedium = ends?.from?.medium?.label ?? ends?.to?.medium?.label

  return (
    <div className="draft-panel" onClick={(e) => e.stopPropagation()}>
      <div className="draft-panel-head">
        <strong>{isNew ? 'New ' : ''}{eq ? 'equipment' : s223 ? 'connection' : 'feeds'}</strong>
        <span className="spacer" />
        <button className="link" onClick={onClose} title="Close">×</button>
      </div>
      {mine.length > 0 && <ul className="draft-problems">{mine.map((p, i) => <li key={i}>{p.message}</li>)}</ul>}
      {eq && <dl>
        <dt>Name</dt>
        <dd><input ref={name} className="cell-input" value={eq.label} autoFocus={isNew}
          onChange={(e) => set('label', { value: e.target.value, label: e.target.value })} />{was('label', (r) => r.label)}</dd>
        <dt>Type</dt>
        <dd>{term('type', 'equipment', eq.type?.label, 'choose a type', false)}{was('type', (r) => r.type?.label)}</dd>
        {hasProcess && <>
          <dt>Process</dt>
          <dd>{term('process', 'process', eq.process?.label, '—', true)}{was('process', (r) => r.process?.label)}</dd>
        </>}
      </dl>}
      {cx && <dl>
        <dt>Name</dt>
        <dd><input className="cell-input" value={newCx ? newCx.label : cx.label} placeholder={newCx ? cx.label : undefined}
          onChange={(e) => set('label', { value: e.target.value, label: e.target.value })} />{was('label', (r) => r.label)}</dd>
        <dt>From</dt>
        <dd><EquipmentSelect value={cx.from_equipment?.id ?? ''} equipment={equipment} exclude={cx.to_equipment?.id}
          onChange={(v) => set('from_equipment', { value: v, label: labelOf(v) })} />{was('from_equipment', (r) => r.from_equipment?.label)}</dd>
        <dt>To</dt>
        <dd><EquipmentSelect value={cx.to_equipment?.id ?? ''} equipment={equipment} exclude={cx.from_equipment?.id}
          onChange={(v) => set('to_equipment', { value: v, label: labelOf(v) })} />{was('to_equipment', (r) => r.to_equipment?.label)}</dd>
        {s223 && <>
          <dt>Medium</dt>
          <dd>{term('medium', 'medium', cx.medium?.label, portMedium ? `${portMedium} (from its connection points)` : 'choose a medium', false)}
            {was('medium', (r) => r.medium?.label)}</dd>
          <dt>Kind</dt>
          <dd>{term('type', 'connection', cx.type?.label, isNew ? 'Pipe (default)' : '—', false)}{was('type', (r) => r.type?.label)}</dd>
        </>}
        {s223 && newCx && <PortFields model={model} draft={draft} id={id} />}
      </dl>}
      <div className="draft-panel-foot">
        {isNew
          ? <button className="link" onClick={() => { setGraphDraft(removeItem(draft, id)); onClose() }}>Remove from draft</button>
          : draft.edits[id] && <button className="link" onClick={() => setGraphDraft(removeItem(draft, id))}>Revert changes</button>}
      </div>
    </div>
  )
}

function EquipmentSelect({ value, equipment, exclude, onChange }: {
  value: string; equipment: EquipmentRow[]; exclude?: string; onChange: (id: string) => void
}) {
  return <select className="cell-input" value={value} onChange={(e) => onChange(e.target.value)}>
    {equipment.filter((e) => e.id !== exclude).map((e) => <option key={e.id} value={e.id}>{e.label || e.id}</option>)}
  </select>
}

/** Which connection points a new connection joins: by default the equipment's one free matching
 * point, else a new one (as the connection mints it); or a chosen free point. */
function PortFields({ model, draft, id }: { model: ModelResponse; draft: Draft; id: string }) {
  const setGraphDraft = useStore((s) => s.setGraphDraft)
  const c = draft.connections.find((x) => x.key === id)!
  const assigned = assignPorts(model, draft).get(id)!
  const field = (end: 'from' | 'to', title: string, kind: string) => {
    const options = portOptions(model, draft, id, end)
    return <>
      <dt>{title}</dt>
      <dd><select className="cell-input" value={c[`${end}_point`]}
        onChange={(e) => setGraphDraft(setPort(draft, id, end, e.target.value))}>
        <option value="auto">{assigned[end] ? `Automatic: ${assigned[end]!.label}` : `Automatic: new ${kind}`}</option>
        <option value="new">New {kind}</option>
        {options.map((p) => <option key={p.id} value={p.id}>{p.label}{p.medium ? ` · ${p.medium.label}` : ''}</option>)}
      </select></dd>
    </>
  }
  return <>{field('from', 'From point', 'outlet')}{field('to', 'To point', 'inlet')}</>
}
