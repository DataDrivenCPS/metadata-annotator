import { useMemo, useState, type ReactNode } from 'react'
import type { Column } from '../selection'
import { useStore } from '../store'
import type { ConnectionPointRow, ConnectionRow, EquipmentRow, PointRow, SpaceRow } from '../types'
import { DataTable } from './DataTable'
import { TermPicker } from './TermPicker'
import { Select, TextEditor } from './cells'
import { useExtraColumns } from './viewColumns'

const POINT_KINDS = [
  ['measurement', 'Measurement'], ['setpoint', 'Setpoint / command value'],
  ['status', 'Status'], ['command', 'On/off or mode command'],
] as const

function useShared() {
  const model = useStore((s) => s.model)!
  const proposal = useStore((s) => s.proposal)
  const edit = useStore((s) => s.edit)
  const issueCounts = useMemo(() => {
    const m = new Map<string, number>()
    for (const i of model.issues) {
      if (i.resolution_state !== 'open' || i.severity === 'suggestion') continue
      for (const id of i.affected_ids) m.set(id, (m.get(id) ?? 0) + 1)
    }
    return m
  }, [model])
  const highlighted = useMemo(
    () => new Set(proposal && proposal.status === 'pending' ? proposal.changes.map((c) => c.entity_id) : []), [proposal])
  const brick = model.info.family === 'brick'
  const hasProcess = model.info.profile === 'watr'
  return { model, edit, issueCounts, highlighted, brick, hasProcess }
}

// ------------------------------------------------------------------ points

export function PointsTable() {
  const { model, edit, issueCounts, highlighted, brick } = useShared()
  const extra = useExtraColumns<PointRow>('points')
  const equipmentOptions = useMemo(
    () => [['', '— unassigned —'] as const, ...model.view.equipment.map((e) => [e.id, e.label] as const)], [model])
  const columns: Column<PointRow>[] = useMemo(() => brick ? [
    { key: 'label', header: 'Point', field: 'label', value: (r) => r.label, width: 200 },
    { key: 'equipment', header: 'Equipment', field: 'equipment', value: (r) => r.equipment?.label ?? '' },
    { key: 'point_type', header: 'Point type', field: 'point_type', value: (r) => r.point_type?.label ?? '' },
    { key: 'point_kind', header: 'Kind', value: (r) => r.point_kind_label, width: 110 },
    { key: 'unit', header: 'Unit', field: 'unit', value: (r) => r.unit ? `${r.unit.label}${r.unit_symbol ? ` (${r.unit_symbol})` : ''}` : '' },
  ] : [
    { key: 'label', header: 'Point', field: 'label', value: (r) => r.label, width: 130 },
    { key: 'equipment', header: 'Equipment', field: 'equipment', value: (r) => r.equipment?.label ?? '' },
    { key: 'point_kind', header: 'Kind', field: 'point_kind', value: (r) => r.point_kind_label, width: 120 },
    { key: 'quantity_kind', header: 'Measurement', field: 'quantity_kind', value: (r) => r.quantity_kind?.label ?? '' },
    { key: 'unit', header: 'Unit', field: 'unit', value: (r) => r.unit ? `${r.unit.label}${r.unit_symbol ? ` (${r.unit_symbol})` : ''}` : '' },
    { key: 'sensor_type', header: 'Sensor type', field: 'sensor_type', value: (r) => r.sensor_type?.label ?? '' },
    { key: 'medium', header: 'Medium', field: 'medium', value: (r) => r.medium?.label ?? '' },
  ], [])

  const editor = (r: PointRow, c: Column<PointRow>, done: () => void): ReactNode => {
    const commit = (value: unknown) => { done(); void edit([{ op: 'update_point', id: r.id, [c.field!]: value }]) }
    switch (c.field) {
      case 'label': return <TextEditor initial={r.label} onCommit={commit} onCancel={done} />
      case 'equipment': return <Select value={r.equipment?.id ?? ''} options={equipmentOptions}
        onCommit={(v) => commit(v || null)} onCancel={done} />
      case 'point_kind': return <Select value={r.point_kind} options={POINT_KINDS} onCommit={commit} onCancel={done} />
      case 'point_type': return <TermPicker kind="point_class" allowClear={false} initial={r.point_type?.label} onPick={commit} onCancel={done} />
      case 'unit': return <TermPicker kind="unit" quantityKind={brick ? null : r.quantity_kind?.iri} onPick={commit} onCancel={done} />
      case 'quantity_kind': return <TermPicker kind="quantity_kind" initial={r.quantity_kind?.label} onPick={commit} onCancel={done} />
      case 'sensor_type': return <TermPicker kind="sensor" onPick={commit} onCancel={done} allowClear={false} />
      case 'medium': return <TermPicker kind="medium" onPick={commit} onCancel={done} />
      default: return null
    }
  }
  return <DataTable rows={model.view.points} columns={[...columns, ...extra.columns]} issueCounts={issueCounts} highlighted={highlighted}
    editor={(r, c, done) => extra.editor?.(r, c, done) ?? editor(r, c, done)} empty="No points yet. Upload a point list in Sources, import an existing model, or add one with + Point."
    toolbar={<AddPoint brick={brick} />} />
}

function AddPoint({ brick }: { brick: boolean }) {
  const edit = useStore((s) => s.edit)
  const [label, setLabel] = useState<string | null>(null)
  if (label === null) return <button onClick={() => setLabel('')}>+ Point</button>
  if (!label) return <TextEditor initial="" onCancel={() => setLabel(null)}
    onCommit={(l) => { if (brick) setLabel(l); else { setLabel(null); void edit([{ op: 'create_point', label: l, point_kind: 'measurement' }]) } }} />
  // Brick: a point is defined by its type, so ask for it right away.
  return <TermPicker kind="point_class" allowClear={false} onCancel={() => setLabel(null)}
    onPick={(point_type) => { setLabel(null); if (point_type) void edit([{ op: 'create_point', label, point_type }]) }} />
}

// --------------------------------------------------------------- equipment

export function EquipmentTable() {
  const { model, edit, issueCounts, highlighted, hasProcess } = useShared()
  const extra = useExtraColumns<EquipmentRow>('equipment')
  const columns: Column<EquipmentRow>[] = useMemo(() => [
    { key: 'label', header: 'Equipment', field: 'label', value: (r) => r.label },
    { key: 'type', header: 'Type', field: 'type', value: (r) => r.type?.label ?? '' },
    ...(hasProcess ? [{ key: 'process', header: 'Treatment process', field: 'process', value: (r: EquipmentRow) => r.process?.label ?? '' }] : []),
    { key: 'points', header: 'Points', value: (r) => String(r.point_count), width: 70 },
    { key: 'contained_in', header: 'Part of', value: (r) => r.contained_in?.label ?? '' },
    { key: 'location', header: 'Location', field: 'location', value: (r: EquipmentRow) => r.location?.label ?? '' },
  ], [hasProcess])
  const spaceOptions = useMemo(() => [['', '— none —'] as const, ...model.view.spaces.map((s) => [s.id, s.label] as const)], [model])
  const editor = (r: EquipmentRow, c: Column<EquipmentRow>, done: () => void): ReactNode => {
    const commit = (value: unknown) => { done(); void edit([{ op: 'update_equipment', id: r.id, [c.field!]: value }]) }
    if (c.field === 'label') return <TextEditor initial={r.label} onCommit={commit} onCancel={done} />
    if (c.field === 'type') return <TermPicker kind="equipment" allowClear={false} onPick={commit} onCancel={done} />
    if (c.field === 'process') return <TermPicker kind="process" onPick={commit} onCancel={done} />
    if (c.field === 'location') return <Select value={r.location?.id ?? ''} options={spaceOptions} onCommit={(v) => commit(v || null)} onCancel={done} />
    return null
  }
  return <DataTable rows={model.view.equipment} columns={[...columns, ...extra.columns]} issueCounts={issueCounts} highlighted={highlighted}
    editor={(r, c, done) => extra.editor?.(r, c, done) ?? editor(r, c, done)} empty="No equipment yet. Import a model, add one with + Equipment, or ask the assistant." toolbar={<AddEquipment />} />
}

function AddEquipment() {
  const edit = useStore((s) => s.edit)
  const [label, setLabel] = useState<string | null>(null)
  if (label === null) return <button onClick={() => setLabel('')}>+ Equipment</button>
  if (!label) return <TextEditor initial="" onCancel={() => setLabel(null)} onCommit={setLabel} />
  return <TermPicker kind="equipment" allowClear={false} initial="" onCancel={() => setLabel(null)}
    onPick={(type) => { setLabel(null); if (type) void edit([{ op: 'create_equipment', label, type }]) }} />
}

// -------------------------------------------------------------- connections

export function ConnectionsTable() {
  const { model, edit, issueCounts, highlighted, brick } = useShared()
  const extra = useExtraColumns<ConnectionRow>('connections')
  const eqOptions = useMemo(() => model.view.equipment.map((e) => [e.id, e.label] as const), [model])
  const columns: Column<ConnectionRow>[] = useMemo(() => [
    { key: 'label', header: 'Connection', field: 'label', value: (r) => r.label, width: 110 },
    { key: 'from', header: 'From (upstream)', field: 'from_equipment', value: (r) => r.from_equipment?.label ?? '' },
    { key: 'to', header: 'Connected to (downstream)', field: 'to_equipment', value: (r) => r.to_equipment?.label ?? '' },
    ...(brick ? [] : [
      { key: 'medium', header: 'Medium', field: 'medium', value: (r: ConnectionRow) => r.medium?.label ?? '' },
      { key: 'type', header: 'Kind', field: 'type', value: (r: ConnectionRow) => r.type?.label ?? '', width: 90 },
      { key: 'from_point', header: 'From point', field: 'from_point', value: (r: ConnectionRow) => r.from_point?.label ?? '' },
      { key: 'to_point', header: 'To point', field: 'to_point', value: (r: ConnectionRow) => r.to_point?.label ?? '' },
    ]),
  ], [brick])
  // Free connection points an end can move to: right direction, not joined by another connection.
  const pointOptions = (r: ConnectionRow, direction: 'outlet' | 'inlet') => model.view.connection_points
    .filter((p) => (p.direction === direction || p.direction === 'bidirectional') && (!p.connection || p.connection.id === r.id))
    .map((p) => [p.id, p.label] as const)
  const editor = (r: ConnectionRow, c: Column<ConnectionRow>, done: () => void): ReactNode => {
    const commit = (value: unknown) => { done(); void edit([{ op: 'update_connection', id: r.id, [c.field!]: value }]) }
    if (c.field === 'from_point') return <Select value={r.from_point?.id ?? ''} options={pointOptions(r, 'outlet')} onCommit={commit} onCancel={done} />
    if (c.field === 'to_point') return <Select value={r.to_point?.id ?? ''} options={pointOptions(r, 'inlet')} onCommit={commit} onCancel={done} />
    if (c.field === 'label') return <TextEditor initial={r.label} onCommit={commit} onCancel={done} />
    if (c.field === 'from_equipment') return <Select value={r.from_equipment?.id ?? ''} options={eqOptions} onCommit={commit} onCancel={done} />
    if (c.field === 'to_equipment') return <Select value={r.to_equipment?.id ?? ''} options={eqOptions} onCommit={commit} onCancel={done} />
    if (c.field === 'medium') return <TermPicker kind="medium" allowClear={false} onPick={commit} onCancel={done} />
    if (c.field === 'type') return <TermPicker kind="connection" allowClear={false} onPick={commit} onCancel={done} />
    return null
  }
  return <DataTable rows={model.view.connections} columns={[...columns, ...extra.columns]} relationship issueCounts={issueCounts}
    highlighted={highlighted} editor={(r, c, done) => extra.editor?.(r, c, done) ?? editor(r, c, done)} empty={brick ? 'No feeds relationships yet.' : 'No connections yet.'}
    toolbar={<AddConnection brick={brick} />} />
}

function AddConnection({ brick }: { brick: boolean }) {
  const model = useStore((s) => s.model)!
  const edit = useStore((s) => s.edit)
  const [draft, setDraft] = useState<{ from: string; to: string } | null>(null)
  if (!draft) return <button onClick={() => setDraft({ from: '', to: '' })} disabled={model.view.equipment.length < 2}>+ Connection</button>
  const eq = model.view.equipment
  return (
    <span className="inline-form">
      <select value={draft.from} onChange={(e) => setDraft({ ...draft, from: e.target.value })}>
        <option value="">from…</option>{eq.map((e) => <option key={e.id} value={e.id}>{e.label}</option>)}
      </select>
      <select value={draft.to} onChange={(e) => setDraft({ ...draft, to: e.target.value })}>
        <option value="">to…</option>{eq.map((e) => <option key={e.id} value={e.id}>{e.label}</option>)}
      </select>
      {draft.from && draft.to && draft.from !== draft.to && brick
        ? <button className="primary" onClick={() => { setDraft(null); void edit([{ op: 'create_connection', from_equipment: draft.from, to_equipment: draft.to }]) }}>Add</button>
        : draft.from && draft.to && draft.from !== draft.to
        ? <TermPicker kind="medium" allowClear={false} onCancel={() => setDraft(null)}
            onPick={(medium) => { setDraft(null); if (medium) void edit([{ op: 'create_connection', from_equipment: draft.from, to_equipment: draft.to, medium }]) }} />
        : <button onClick={() => setDraft(null)}>Cancel</button>}
    </span>
  )
}

// -------------------------------------------------------- connection points

const DIRECTIONS = [['inlet', 'Inlet'], ['outlet', 'Outlet'], ['bidirectional', 'Bidirectional']] as const
const NONE = ['', '— none —'] as const

export function ConnectionPointsTable() {
  const { model, edit, issueCounts, highlighted } = useShared()
  const extra = useExtraColumns<ConnectionPointRow>('connection_points')
  const eqOptions = useMemo(() => model.view.equipment.map((e) => [e.id, e.label] as const), [model])
  const columns: Column<ConnectionPointRow>[] = useMemo(() => [
    { key: 'label', header: 'Connection point', field: 'label', value: (r) => r.label, width: 240 },
    { key: 'equipment', header: 'Equipment', field: 'equipment', value: (r) => r.equipment?.label ?? '' },
    { key: 'direction', header: 'Direction', field: 'direction', value: (r) => DIRECTIONS.find(([d]) => d === r.direction)?.[1] ?? r.direction, width: 100 },
    { key: 'medium', header: 'Medium', field: 'medium', value: (r) => r.medium?.label ?? '', width: 90 },
    { key: 'connection', header: 'Connection', value: (r) => r.connection?.label ?? '' },
    { key: 'paired_with', header: 'Paired with', field: 'paired_with', value: (r) => r.paired_with?.label ?? '' },
    { key: 'maps_to', header: 'Maps to (container)', field: 'maps_to', value: (r) => r.maps_to?.label ?? '' },
  ], [])
  const editor = (r: ConnectionPointRow, c: Column<ConnectionPointRow>, done: () => void): ReactNode => {
    const commit = (value: unknown) => { done(); void edit([{ op: 'update_connection_point', id: r.id, [c.field!]: value }]) }
    const points = model.view.connection_points
    switch (c.field) {
      case 'label': return <TextEditor initial={r.label} onCommit={commit} onCancel={done} />
      case 'equipment': return <Select value={r.equipment?.id ?? ''} options={eqOptions} onCommit={commit} onCancel={done} />
      case 'direction': return <Select value={r.direction} options={DIRECTIONS} onCommit={commit} onCancel={done} />
      case 'medium': return <TermPicker kind="medium" allowClear={false} initial={r.medium?.label} onPick={commit} onCancel={done} />
      case 'paired_with': {
        // An inlet pairs with an outlet of the same equipment (one per flow path).
        const opposite = r.direction === 'inlet' ? 'outlet' : r.direction === 'outlet' ? 'inlet' : null
        const options = points.filter((p) => p.id !== r.id && p.equipment?.id === r.equipment?.id && p.direction === opposite)
          .map((p) => [p.id, p.label] as const)
        return <Select value={r.paired_with?.id ?? ''} options={[NONE, ...options]} onCommit={(v) => commit(v || null)} onCancel={done} />
      }
      case 'maps_to': {
        // A contained equipment's point maps to a point of its container.
        const container = model.view.equipment.find((e) => e.id === r.equipment?.id)?.contained_in?.id
        const options = points.filter((p) => container && p.equipment?.id === container).map((p) => [p.id, p.label] as const)
        return <Select value={r.maps_to?.id ?? ''} options={[NONE, ...options]} onCommit={(v) => commit(v || null)} onCancel={done} />
      }
      default: return null
    }
  }
  return <DataTable rows={model.view.connection_points} columns={[...columns, ...extra.columns]} issueCounts={issueCounts} highlighted={highlighted}
    editor={(r, c, done) => extra.editor?.(r, c, done) ?? editor(r, c, done)} toolbar={<AddConnectionPoint />}
    empty="No connection points yet. Each connection adds an outlet and an inlet; add others with + Connection point." />
}

function AddConnectionPoint() {
  const model = useStore((s) => s.model)!
  const edit = useStore((s) => s.edit)
  const [draft, setDraft] = useState<{ equipment: string; direction: string } | null>(null)
  if (!draft) return <button onClick={() => setDraft({ equipment: '', direction: '' })} disabled={!model.view.equipment.length}>+ Connection point</button>
  return (
    <span className="inline-form">
      <select value={draft.equipment} onChange={(e) => setDraft({ ...draft, equipment: e.target.value })}>
        <option value="">equipment…</option>{model.view.equipment.map((e) => <option key={e.id} value={e.id}>{e.label}</option>)}
      </select>
      <select value={draft.direction} onChange={(e) => setDraft({ ...draft, direction: e.target.value })}>
        <option value="">direction…</option>{DIRECTIONS.map(([d, l]) => <option key={d} value={d}>{l}</option>)}
      </select>
      {draft.equipment && draft.direction
        ? <TermPicker kind="medium" allowClear={false} onCancel={() => setDraft(null)}
            onPick={(medium) => { setDraft(null); if (medium) void edit([{ op: 'create_connection_point', equipment: draft.equipment, direction: draft.direction, medium }]) }} />
        : <button onClick={() => setDraft(null)}>Cancel</button>}
    </span>
  )
}

// ------------------------------------------------------------------- spaces

export function SpacesTable() {
  const { model, edit, issueCounts, highlighted } = useShared()
  const extra = useExtraColumns<SpaceRow>('spaces')
  const columns: Column<SpaceRow>[] = useMemo(() => [
    { key: 'label', header: 'Space', field: 'label', value: (r) => r.label },
    { key: 'type', header: 'Type', field: 'type', value: (r) => r.type?.label ?? '' },
    { key: 'part_of', header: 'Part of', field: 'part_of', value: (r) => r.part_of?.label ?? '' },
    { key: 'equipment', header: 'Equipment', value: (r) => String(r.equipment_count), width: 90 },
  ], [])
  const editor = (r: SpaceRow, c: Column<SpaceRow>, done: () => void): ReactNode => {
    const commit = (value: unknown) => { done(); void edit([{ op: 'update_space', id: r.id, [c.field!]: value }]) }
    if (c.field === 'label') return <TextEditor initial={r.label} onCommit={commit} onCancel={done} />
    if (c.field === 'type') return <TermPicker kind="location" allowClear={false} initial={r.type?.label} onPick={commit} onCancel={done} />
    if (c.field === 'part_of') {
      const options = [['', '— none —'] as const, ...model.view.spaces.filter((s) => s.id !== r.id).map((s) => [s.id, s.label] as const)]
      return <Select value={r.part_of?.id ?? ''} options={options} onCommit={(v) => commit(v || null)} onCancel={done} />
    }
    return null
  }
  return <DataTable rows={model.view.spaces} columns={[...columns, ...extra.columns]} issueCounts={issueCounts} highlighted={highlighted}
    editor={(r, c, done) => extra.editor?.(r, c, done) ?? editor(r, c, done)} toolbar={<AddSpace />}
    empty="No spaces yet. Add buildings, floors and rooms with + Space, then set each equipment's Location." />
}

function AddSpace() {
  const edit = useStore((s) => s.edit)
  const [label, setLabel] = useState<string | null>(null)
  if (label === null) return <button onClick={() => setLabel('')}>+ Space</button>
  if (!label) return <TextEditor initial="" onCancel={() => setLabel(null)} onCommit={setLabel} />
  return <TermPicker kind="location" allowClear={false} initial="" onCancel={() => setLabel(null)}
    onPick={(type) => { setLabel(null); if (type) void edit([{ op: 'create_space', label, type }]) }} />
}
