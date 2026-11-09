import dagre from '@dagrejs/dagre'
import {
  Background, BaseEdge, Controls, EdgeLabelRenderer, Handle, MarkerType, Position, ReactFlow, ReactFlowProvider, useReactFlow,
  type Connection, type Edge, type EdgeProps, type Node, type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import {
  addConnection, addEquipment, compileDraft, draftConnectionRows, draftCount, draftEquipmentRows, isDraftId, moveEquipment, setField,
} from '../draft'
import { isSelected } from '../selection'
import { useStore } from '../store'
import type { EquipmentRow, SpaceRow } from '../types'
import { DraftBar, DraftPanel, EQUIPMENT_DRAG, PencilIcon } from './DraftPanel'

const W = 190
const H = 64

/** How the draft touches a drawn object: drafted anew, or an existing object with drafted edits. */
type DraftState = 'new' | 'edited' | null
type EqNodeData = { row: EquipmentRow; issues: number; proposed: boolean; draft: DraftState; problem: boolean }

const draftClass = (draft: DraftState, problem: boolean) => `${draft ? `draft-${draft}` : ''} ${problem ? 'draft-problem' : ''}`

function EquipmentNode({ data, selected }: NodeProps<Node<EqNodeData>>) {
  const { row, issues, proposed, draft, problem } = data
  return (
    <div className={`eq-node ${selected ? 'selected' : ''} ${proposed ? 'proposed' : ''} ${draftClass(draft, problem)}`}>
      <Handle type="target" position={Position.Left} />
      <div className="eq-label">{row.label || <span className="muted">unnamed</span>}</div>
      <div className="eq-type">{row.type?.label ?? (draft === 'new' ? 'choose a type' : 'Untyped')}{row.point_count ? ` · ${row.point_count} pts` : ''}</div>
      {issues > 0 && <span className="badge warn eq-badge" title="Open issues">{issues}</span>}
      <Handle type="source" position={Position.Right} />
    </div>
  )
}
type SpaceNodeData = { row: SpaceRow; issues: number; proposed: boolean }

function SpaceNode({ data, selected }: NodeProps<Node<SpaceNodeData>>) {
  const { row, issues, proposed } = data
  return (
    <div className={`space-node ${selected ? 'selected' : ''} ${proposed ? 'proposed' : ''}`}>
      <Handle type="target" position={Position.Left} />
      <div className="eq-label">{row.label}</div>
      <div className="eq-type">{row.type?.label ?? 'Space'}{row.equipment_count ? ` · ${row.equipment_count} equipment` : ''}</div>
      {issues > 0 && <span className="badge warn eq-badge" title="Open issues">{issues}</span>}
      <Handle type="source" position={Position.Right} />
    </div>
  )
}
const nodeTypes = { equipment: EquipmentNode, space: SpaceNode }

/** A curved edge bent sideways by ``offset`` so parallel connections stay distinguishable. */
function ParallelEdge(props: EdgeProps<Edge<{ offset: number }>>) {
  const { sourceX: sx, sourceY: sy, targetX: tx, targetY: ty, data, markerEnd, style, label, selected } = props
  const off = data?.offset ?? 0
  const mx = (sx + tx) / 2
  const my = (sy + ty) / 2
  const len = Math.hypot(tx - sx, ty - sy) || 1
  const cx = mx - ((ty - sy) / len) * off * 2
  const cy = my + ((tx - sx) / len) * off * 2
  const path = `M ${sx},${sy} Q ${cx},${cy} ${tx},${ty}`
  const lx = (sx + 2 * cx + tx) / 4
  const ly = (sy + 2 * cy + ty) / 4
  return (
    <>
      <BaseEdge id={props.id} path={path} markerEnd={markerEnd} style={style} />
      {label && (
        <EdgeLabelRenderer>
          <div className={`edge-label ${selected ? 'selected' : ''}`}
            style={{ transform: `translate(-50%, -50%) translate(${lx}px, ${ly}px)` }}>{label}</div>
        </EdgeLabelRenderer>
      )}
    </>
  )
}
const edgeTypes = { parallel: ParallelEdge }

function parallelOffsets(pairs: { id: string; a: string; b: string }[]) {
  const groups = new Map<string, string[]>()
  for (const { id, a, b } of pairs) {
    const key = [a, b].sort().join('|')
    groups.set(key, [...(groups.get(key) ?? []), id])
  }
  const out = new Map<string, number>()
  for (const ids of groups.values()) ids.forEach((id, i) => out.set(id, (i - (ids.length - 1) / 2) * 28))
  return out
}

/** Place only nodes without a saved position, so unrelated edits never move the layout.
 * Connected equipment gets a left-to-right flow layout; equipment with no connections
 * (common in Brick models built from point lists) goes in a grid beside it. */
function placeMissing(ids: string[], edges: [string, string][], saved: Record<string, [number, number]>) {
  const missing = ids.filter((id) => !saved[id])
  if (!missing.length) return {}
  const linked = new Set(edges.flat())
  const flow = missing.filter((id) => linked.has(id))
  const loose = missing.filter((id) => !linked.has(id))
  const placed: Record<string, [number, number]> = {}
  const taken = Object.values(saved)
  const collides = (x: number, y: number) => taken.some(([tx, ty]) => Math.abs(tx - x) < W && Math.abs(ty - y) < H + 10)
  const hasSaved = Object.keys(saved).length > 0
  const baseY = hasSaved ? Math.max(...taken.map(([, ty]) => ty)) + H + 40 : 0

  if (flow.length) {
    const g = new dagre.graphlib.Graph()
    g.setGraph({ rankdir: 'LR', nodesep: 40, ranksep: 90 })
    g.setDefaultEdgeLabel(() => ({}))
    const inGraph = ids.filter((id) => linked.has(id))
    for (const id of inGraph) g.setNode(id, { width: W, height: H })
    for (const [a, b] of edges) if (inGraph.includes(a) && inGraph.includes(b)) g.setEdge(a, b)
    dagre.layout(g)
    for (const id of flow) {
      const n = g.node(id)
      let x = n.x - W / 2
      let y = n.y - H / 2 + (hasSaved ? baseY : 0)
      while (collides(x, y)) x += W + 30
      placed[id] = [x, y]
      taken.push([x, y])
    }
  }
  if (loose.length) {
    const cols = Math.max(4, Math.ceil(Math.sqrt(loose.length * 1.6)))
    const flowRight = taken.length ? Math.max(...taken.map(([tx]) => tx)) + W + 80 : 0
    const startX = flow.length || hasSaved ? flowRight : 0
    loose.sort((a, b) => a.localeCompare(b))
    loose.forEach((id, i) => {
      let x = startX + (i % cols) * (W + 30)
      let y = Math.floor(i / cols) * (H + 30)
      while (collides(x, y)) y += H + 30
      placed[id] = [x, y]
      taken.push([x, y])
    })
  }
  return placed
}

/** Containment, space and virtual edges are drawn for context; only connections are model rows here. */
const CONTEXT_EDGE = /^(contains|part_of|location|virtual):/

export function GraphView() {
  return <ReactFlowProvider><GraphCanvas /></ReactFlowProvider>
}

function GraphCanvas() {
  const model = useStore((s) => s.model)!
  const projectId = useStore((s) => s.projectId)!
  const selection = useStore((s) => s.selection)
  const proposal = useStore((s) => s.proposal)
  const click = useStore((s) => s.click)
  const clearSelection = useStore((s) => s.clearSelection)
  const viewing = useStore((s) => s.viewing)
  const graphDraft = useStore((s) => s.graphDraft)
  const startDraft = useStore((s) => s.startDraft)
  const setGraphDraft = useStore((s) => s.setGraphDraft)
  const discardDraft = useStore((s) => s.discardDraft)
  const flow = useReactFlow()
  const wrap = useRef<HTMLDivElement>(null)
  const [positions, setPositions] = useState<Record<string, [number, number]>>(model.layout)
  const [measurements, setMeasurements] = useState<Record<string, Node['measured']>>({})
  const savedRef = useRef(model.layout)
  const spaces = useMemo(() => model.view.spaces ?? [], [model])
  const [showSpaces, setShowSpaces] = useState(true)
  const withSpaces = showSpaces && spaces.length > 0
  // Draft mode: an earlier revision being browsed is read-only, so its draft waits.
  const draft = viewing ? null : graphDraft
  const [focus, setFocus] = useState<string | null>(null)  // the object open in the draft editor
  const [confirmDiscard, setConfirmDiscard] = useState(false)

  // Spaces sit left of what they contain: parent space -> inner space -> equipment located there.
  const spaceLinks = useMemo(() => withSpaces ? [
    ...spaces.filter((s) => s.part_of).map((s) => ['part_of', s.part_of!.id, s.id] as const),
    ...model.view.equipment.filter((e) => e.location).map((e) => ['location', e.location!.id, e.id] as const),
  ] : [], [model, withSpaces, spaces])
  const eqIds = useMemo(() => [...model.view.equipment.map((e) => e.id), ...(withSpaces ? spaces.map((s) => s.id) : [])],
    [model, withSpaces, spaces])
  const edgePairs = useMemo(() => [
    ...model.view.connections.filter((c) => c.from_equipment && c.to_equipment)
      .map((c) => [c.from_equipment!.id, c.to_equipment!.id] as [string, string]),
    ...spaceLinks.map(([, a, b]) => [a, b] as [string, string]),
  ], [model, spaceLinks])

  useEffect(() => {
    const saved = { ...savedRef.current, ...model.layout }
    const placed = placeMissing(eqIds, edgePairs, saved)
    const next = { ...saved, ...placed }
    savedRef.current = next
    setPositions(next)
    if (Object.keys(placed).length) void api.saveLayout(projectId, placed)
  }, [eqIds, edgePairs, model.layout, projectId])

  const issueCounts = useMemo(() => {
    const m = new Map<string, number>()
    for (const i of model.issues) if (i.resolution_state === 'open' && i.severity !== 'suggestion')
      for (const id of i.affected_ids) m.set(id, (m.get(id) ?? 0) + 1)
    return m
  }, [model])
  const proposed = useMemo(() => new Set(proposal?.status === 'pending' ? proposal.changes.map((c) => c.entity_id) : []), [proposal])

  // What is drawn: the model, with the draft (if any) overlaid on it.
  const equipment = useMemo(() => draft ? draftEquipmentRows(model, draft) : model.view.equipment, [model, draft])
  const connections = useMemo(() => draft ? draftConnectionRows(model, draft) : model.view.connections, [model, draft])
  const problems = useMemo(() => draft ? compileDraft(model, draft).problems : [], [model, draft])
  const problemIds = new Set(problems.map((p) => p.id))
  const equipmentIds = new Set(equipment.map((e) => e.id))
  const draftState = (id: string): DraftState => !draft ? null : isDraftId(id) ? 'new' : draft.edits[id] ? 'edited' : null
  const draftPositions = new Map(draft?.equipment.map((e) => [e.key, e.position]) ?? [])
  const focused = draft && focus && (equipmentIds.has(focus) || connections.some((c) => c.id === focus)) ? focus : null
  const labelOf = (id: string) => equipment.find((e) => e.id === id)?.label || connections.find((c) => c.id === id)?.label || id

  const at = (id: string) => {
    const p = positions[id] ?? draftPositions.get(id)
    return { x: p?.[0] ?? 0, y: p?.[1] ?? 0 }
  }
  const nodes: Node<EqNodeData | SpaceNodeData>[] = [
    ...equipment.map((row) => ({
      id: row.id, type: 'equipment', position: at(row.id), measured: measurements[row.id],
      data: { row, issues: issueCounts.get(row.id) ?? 0, proposed: proposed.has(row.id), draft: draftState(row.id), problem: problemIds.has(row.id) },
      selected: isSelected(selection, row.id) || focused === row.id,
    })),
    ...(withSpaces ? spaces.map((row) => ({
      id: row.id, type: 'space', position: at(row.id), measured: measurements[row.id], connectable: false,
      data: { row, issues: issueCounts.get(row.id) ?? 0, proposed: proposed.has(row.id) },
      selected: isSelected(selection, row.id),
    })) : []),
  ]
  const shown = new Set(nodes.map((n) => n.id))
  const virtualEdges = (model.view.relationships ?? []).filter((r) => r.virtual && r.object
    && shown.has(r.subject.id) && shown.has(r.object.id))
  const drawn = connections.filter((c) => c.from_equipment && c.to_equipment)
  const offsets = parallelOffsets(drawn.map((c) => ({ id: c.id, a: c.from_equipment!.id, b: c.to_equipment!.id })))
  const edges: Edge[] = [
    ...drawn.map((c) => ({
      id: c.id, source: c.from_equipment!.id, target: c.to_equipment!.id,
      type: 'parallel', data: { offset: offsets.get(c.id) ?? 0 },
      label: `${c.label}${c.medium ? ` · ${c.medium.label.replace(/^.*?: /, '')}` : ''}`,
      selected: isSelected(selection, c.id) || focused === c.id,
      markerEnd: c.directed ? { type: MarkerType.ArrowClosed } : undefined,
      className: `${proposed.has(c.id) ? 'proposed' : ''} ${(issueCounts.get(c.id) ?? 0) > 0 ? 'has-issue' : ''} ${draftClass(draftState(c.id), problemIds.has(c.id))}`,
      animated: proposed.has(c.id),
    })),
    ...model.view.containment.map(([parent, child]) => ({
      id: `contains:${parent}>${child}`, source: parent, target: child, selectable: false, reconnectable: false,
      style: { strokeDasharray: '4 4' }, label: 'contains',
    })),
    ...spaceLinks.map(([kind, space, inner]) => ({
      id: `${kind}:${space}>${inner}`, source: space, target: inner, selectable: false, reconnectable: false,
      className: `space-edge ${kind}`, label: kind === 'part_of' ? 'contains' : 'in',
    })),
    // Virtual relations between drawn nodes (rooms adjacent to each other...); not used for the layout.
    ...virtualEdges.map((r) => ({
      id: `virtual:${r.id}`, source: r.subject.id, target: r.object!.id, selectable: false, reconnectable: false,
      className: 'space-edge virtual', label: r.relation.label,
    })),
  ]

  const unassigned = model.view.points.filter((p) => !p.equipment).length

  // ----------------------------------------------------------- draft gestures
  const current = () => useStore.getState().graphDraft
  const connect = (c: Connection) => {
    const d = current()
    if (!d || !c.source || !c.target || c.source === c.target) return
    const [next, key] = addConnection(d, c.source, c.target)
    setGraphDraft(next)
    setFocus(key)
  }
  const reconnect = (old: Edge, c: Connection) => {
    let d = current()
    if (!d || !c.source || !c.target || c.source === c.target) return
    if (c.source !== old.source) d = setField(model, d, old.id, 'from_equipment', { value: c.source, label: labelOf(c.source) })
    if (c.target !== old.target) d = setField(model, d, old.id, 'to_equipment', { value: c.target, label: labelOf(c.target) })
    setGraphDraft(d)
    setFocus(old.id)
  }
  /** New equipment centred on a point in graph coordinates. */
  const place = (x: number, y: number) => {
    const d = current()
    if (!d) return
    const [next, key] = addEquipment(d, [Math.round(x - W / 2), Math.round(y - H / 2)])
    setGraphDraft(next)
    setFocus(key)
  }
  const placeInMiddle = () => {
    const r = wrap.current?.getBoundingClientRect()
    if (!r) return
    const p = flow.screenToFlowPosition({ x: r.left + r.width / 2, y: r.top + r.height / 2 })
    const nudge = ((current()?.equipment.length ?? 0) % 6) * 24  // repeated clicks don't stack exactly
    place(p.x + nudge, p.y + nudge)
  }
  const begin = () => {
    startDraft()
    setFocus(null)
    setConfirmDiscard(false)
  }
  /** Open an object in the draft editor, starting a draft if there is none. */
  const edit = (id: string) => {
    if (viewing) return
    if (!draft) begin()
    setFocus(id)
  }
  const leaveDraft = () => {
    if (draft && draftCount(draft)) setConfirmDiscard(true)
    else { discardDraft(); setFocus(null) }
  }

  return (
    <div className={`graph-wrap ${draft ? 'drafting' : ''}`} ref={wrap}>
      {unassigned > 0 && !draft && <div className="graph-note">{unassigned} point(s) are not assigned to equipment — see the Points tab.</div>}
      {draft && <DraftBar draft={draft} problems={problems} labelOf={labelOf} onFocus={setFocus}
        onAddEquipment={placeInMiddle} confirming={confirmDiscard} setConfirming={setConfirmDiscard} />}
      <div className="graph-tools">
        {spaces.length > 0 && <label className="graph-toggle">
          <input type="checkbox" checked={showSpaces} onChange={(e) => setShowSpaces(e.target.checked)} /> Show spaces ({spaces.length})
        </label>}
        {!viewing && <button className={`draft-toggle ${draft ? 'active' : ''}`} aria-pressed={!!draft}
          title={draft ? 'Leave draft mode'
            : 'Draft changes: draw connections, add equipment and edit names, types, media and processes, then check and save them together as one edit'}
          onClick={() => (draft ? leaveDraft() : begin())}>
          <PencilIcon /> {draft ? 'Drafting' : 'Draft'}
        </button>}
      </div>
      {draft && focused && <DraftPanel key={focused} id={focused} model={model} draft={draft} equipment={equipment}
        connections={connections} problems={problems} onClose={() => setFocus(null)} />}
      <ReactFlow
        nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} fitView minZoom={0.2}
        nodesConnectable={!!draft} edgesReconnectable={!!draft} elementsSelectable zoomOnDoubleClick={false} deleteKeyCode={null}
        isValidConnection={(c) => c.source !== c.target && equipmentIds.has(c.source) && equipmentIds.has(c.target)}
        onConnect={draft ? connect : undefined}
        onReconnect={draft ? reconnect : undefined}
        onDragOver={(e) => {
          if (draft && e.dataTransfer.types.includes(EQUIPMENT_DRAG)) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy' }
        }}
        onDrop={(e) => {
          if (!draft || !e.dataTransfer.types.includes(EQUIPMENT_DRAG)) return
          e.preventDefault()
          const p = flow.screenToFlowPosition({ x: e.clientX, y: e.clientY })
          place(p.x, p.y)
        }}
        onNodeClick={(e, n) => {
          if (draft) setFocus(n.type === 'equipment' ? n.id : null)
          if (!isDraftId(n.id)) click({ id: n.id }, { ctrl: e.ctrlKey || e.metaKey, shift: e.shiftKey }, eqIds)
        }}
        onNodeDoubleClick={(_, n) => { if (n.type === 'equipment') edit(n.id) }}
        onEdgeClick={(e, ed) => {
          if (CONTEXT_EDGE.test(ed.id)) return
          if (draft) setFocus(ed.id)
          if (!isDraftId(ed.id)) click({ id: ed.id, relationship: true }, { ctrl: e.ctrlKey || e.metaKey, shift: false }, [])
        }}
        onEdgeDoubleClick={(_, ed) => { if (!CONTEXT_EDGE.test(ed.id)) edit(ed.id) }}
        onPaneClick={() => { clearSelection(); setFocus(null) }}
        onNodesChange={(changes) => {
          const moved: Record<string, [number, number]> = {}
          const measured: Record<string, Node['measured']> = {}
          for (const ch of changes) {
            if (ch.type === 'position' && ch.position) moved[ch.id] = [ch.position.x, ch.position.y]
            if (ch.type === 'dimensions' && ch.dimensions) measured[ch.id] = ch.dimensions
          }
          // Controlled nodes must retain measured sizes: dropping them on a drag
          // resets React Flow's handle bounds and restarts resize observation.
          if (Object.keys(measured).length) setMeasurements((m) => ({ ...m, ...measured }))
          if (Object.keys(moved).length) setPositions((p) => ({ ...p, ...moved }))
        }}
        onNodeDragStop={(_, __, dragged) => {
          const moved: Record<string, [number, number]> = {}
          let d = current()
          const drafted: string[] = []
          for (const node of dragged) {
            const at: [number, number] = [node.position.x, node.position.y]
            if (isDraftId(node.id)) { if (d) d = moveEquipment(d, node.id, at); drafted.push(node.id) }
            else moved[node.id] = at
          }
          // A drafted node's position lives in the draft until it is saved under its real id.
          if (d && drafted.length) {
            setGraphDraft(d)
            setPositions((p) => Object.fromEntries(Object.entries(p).filter(([id]) => !drafted.includes(id))))
          }
          if (Object.keys(moved).length) {
            savedRef.current = { ...savedRef.current, ...moved }
            void api.saveLayout(projectId, moved)
          }
        }}
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  )
}
