import { useRef } from 'react'

/** A drag handle between two panes. ``onResize`` gets the pointer movement since the last
 * call: upward (rows) or rightward (columns) is positive. Double-click calls ``onReset``. */
export function ResizeHandle({ label, onResize, onReset, direction = 'rows' }: {
  label: string
  onResize: (delta: number) => void
  onReset?: () => void
  direction?: 'rows' | 'columns'
}) {
  const start = useRef<number | null>(null)
  const columns = direction === 'columns'
  const position = (event: { clientX: number; clientY: number }) => columns ? event.clientX : -event.clientY
  return <div
    className={`resize-handle ${columns ? 'columns' : 'rows'}`}
    role="separator"
    aria-orientation={columns ? 'vertical' : 'horizontal'}
    aria-label={label}
    aria-valuetext="Drag to resize"
    title={onReset ? 'Drag to resize · double-click to reset' : 'Drag to resize'}
    tabIndex={0}
    onPointerDown={(event) => {
      start.current = position(event)
      event.currentTarget.setPointerCapture(event.pointerId)
    }}
    onPointerMove={(event) => {
      if (start.current !== null) {
        onResize(position(event) - start.current)
        start.current = position(event)
      }
    }}
    onPointerUp={() => { start.current = null }}
    onPointerCancel={() => { start.current = null }}
    onDoubleClick={onReset}
    onKeyDown={(event) => {
      const [grow, shrink] = columns ? ['ArrowRight', 'ArrowLeft'] : ['ArrowUp', 'ArrowDown']
      if (event.key === grow) { event.preventDefault(); onResize(16) }
      if (event.key === shrink) { event.preventDefault(); onResize(-16) }
    }}
  ><span /></div>
}
