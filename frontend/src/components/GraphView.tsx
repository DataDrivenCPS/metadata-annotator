import dagre from '@dagrejs/dagre'
import {
  Background, BaseEdge, Controls, EdgeLabelRenderer, Handle, MarkerType, Position, ReactFlow,
  type Edge, type EdgeProps, type Node, type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { isSelected } from '../selection'
import { useStore } from '../store'
import type { EquipmentRow } from '../types'

const W = 190
const H = 64

type EqNodeData = { row: EquipmentRow; issues: number; proposed: boolean }

function EquipmentNode({ data, selected }: NodeProps<Node<EqNodeData>>) {
  const { row, issues, proposed } = data
  return (
    <div className={`eq-node ${selected ? 'selected' : ''} ${proposed ? 'proposed' : ''}`}>
      <Handle type="target" position={Position.Left} />
      <div className="eq-label">{row.label}</div>
      <div className="eq-type">{row.type?.label ?? 'Untyped'}{row.point_count ? ` · ${row.point_count} pts` : ''}</div>
      {issues > 0 && <span className="badge warn eq-badge" title="Open issues">{issues}</span>}
      <Handle type="source" position={Position.Right} />
    </div>
  )
}
const nodeTypes = { equipment: EquipmentNode }

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

export function GraphView() {
  const model = useStore((s) => s.model)!
  const projectId = useStore((s) => s.projectId)!
  const selection = useStore((s) => s.selection)
  const proposal = useStore((s) => s.proposal)
  const click = useStore((s) => s.click)
  const clearSelection = useStore((s) => s.clearSelection)
  const [positions, setPositions] = useState<Record<string, [number, number]>>(model.layout)
  const savedRef = useRef(model.layout)

  const eqIds = useMemo(() => model.view.equipment.map((e) => e.id), [model])
  const edgePairs = useMemo(() => model.view.connections
    .filter((c) => c.from_equipment && c.to_equipment)
    .map((c) => [c.from_equipment!.id, c.to_equipment!.id] as [string, string]), [model])

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

  const nodes: Node<EqNodeData>[] = model.view.equipment.map((row) => ({
    id: row.id, type: 'equipment', position: { x: positions[row.id]?.[0] ?? 0, y: positions[row.id]?.[1] ?? 0 },
    data: { row, issues: issueCounts.get(row.id) ?? 0, proposed: proposed.has(row.id) },
    selected: isSelected(selection, row.id),
  }))
  const drawn = model.view.connections.filter((c) => c.from_equipment && c.to_equipment)
  const offsets = parallelOffsets(drawn.map((c) => ({ id: c.id, a: c.from_equipment!.id, b: c.to_equipment!.id })))
  const edges: Edge[] = [
    ...drawn.map((c) => ({
      id: c.id, source: c.from_equipment!.id, target: c.to_equipment!.id,
      type: 'parallel', data: { offset: offsets.get(c.id) ?? 0 },
      label: `${c.label}${c.medium ? ` · ${c.medium.label.replace(/^.*?: /, '')}` : ''}`,
      selected: isSelected(selection, c.id),
      markerEnd: c.directed ? { type: MarkerType.ArrowClosed } : undefined,
      className: `${proposed.has(c.id) ? 'proposed' : ''} ${(issueCounts.get(c.id) ?? 0) > 0 ? 'has-issue' : ''}`,
      animated: proposed.has(c.id),
    })),
    ...model.view.containment.map(([parent, child]) => ({
      id: `contains:${parent}>${child}`, source: parent, target: child, selectable: false,
      style: { strokeDasharray: '4 4' }, label: 'contains',
    })),
  ]

  const unassigned = model.view.points.filter((p) => !p.equipment).length

  return (
    <div className="graph-wrap">
      {unassigned > 0 && <div className="graph-note">{unassigned} point(s) are not assigned to equipment — see the Points tab.</div>}
      <ReactFlow
        nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} fitView minZoom={0.2}
        nodesConnectable={false} elementsSelectable
        onNodeClick={(e, n) => click({ id: n.id }, { ctrl: e.ctrlKey || e.metaKey, shift: e.shiftKey }, eqIds)}
        onEdgeClick={(e, ed) => { if (!ed.id.startsWith('contains:'))
          click({ id: ed.id, relationship: true }, { ctrl: e.ctrlKey || e.metaKey, shift: false }, []) }}
        onPaneClick={() => clearSelection()}
        onNodesChange={(changes) => {
          const moved: Record<string, [number, number]> = {}
          for (const ch of changes) if (ch.type === 'position' && ch.position) moved[ch.id] = [ch.position.x, ch.position.y]
          if (Object.keys(moved).length) setPositions((p) => ({ ...p, ...moved }))
        }}
        onNodeDragStop={(_, node) => {
          const pos: [number, number] = [node.position.x, node.position.y]
          savedRef.current = { ...savedRef.current, [node.id]: pos }
          void api.saveLayout(projectId, { [node.id]: pos })
        }}
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  )
}
