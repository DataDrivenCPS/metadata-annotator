import { useRef } from 'react'

export function ResizeHandle({ label, onResize }: { label: string; onResize: (delta: number) => void }) {
  const startY = useRef<number | null>(null)
  return <div
    className="resize-handle"
    role="separator"
    aria-orientation="horizontal"
    aria-label={label}
    aria-valuetext="Drag to resize"
    tabIndex={0}
    onPointerDown={(event) => {
      startY.current = event.clientY
      event.currentTarget.setPointerCapture(event.pointerId)
    }}
    onPointerMove={(event) => {
      if (startY.current !== null) {
        onResize(startY.current - event.clientY)
        startY.current = event.clientY
      }
    }}
    onPointerUp={() => { startY.current = null }}
    onPointerCancel={() => { startY.current = null }}
    onKeyDown={(event) => {
      if (event.key === 'ArrowUp') { event.preventDefault(); onResize(16) }
      if (event.key === 'ArrowDown') { event.preventDefault(); onResize(-16) }
    }}
  ><span /></div>
}
