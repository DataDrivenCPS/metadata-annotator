import { useState, type ReactNode } from 'react'

/** Anything that opens and closes, with one look: a muted summary line with a turning chevron.
 * Give the content as a function to render it only once opened (a closed one costs nothing). */
export function Disclosure({ summary, children, className, summaryClassName, defaultOpen = false }: {
  summary: ReactNode; children: ReactNode | (() => ReactNode); className?: string; summaryClassName?: string
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  return <details className={className ? `disclosure ${className}` : 'disclosure'} open={defaultOpen || undefined}
    onToggle={(e) => setOpen(e.currentTarget.open)}>
    <summary className={summaryClassName}>{summary}</summary>
    {typeof children === 'function' ? open && children() : children}
  </details>
}
