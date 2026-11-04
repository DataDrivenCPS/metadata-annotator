// How the auto-fix report is laid out: groups sorted into sections by what the person has to do,
// each with a one-line headline and the objects it affects. Pure, so it can be tested on its own.
import type { AutofixChoice, AutofixGroup, AutofixStatus, ProposalState } from './assistant'
import type { ReviewIssue, Row } from './types'

interface StatusInfo {
  /** The short tag on each item. */
  badge: string
  /** The section heading. */
  heading: string
  /** One sentence saying what the section means and what to do, for someone new to auto-fix. */
  hint: string
  /** Whether the person still has something to do for this kind of result. */
  needsYou: boolean
  /** What the assistant's explanation is, for this result. */
  explanationLabel: string
  /** What the run's reasons are, for this result (fixed groups have none). */
  reasonsLabel: string
  /** The button that takes the issues to the chat, when the person still has something to do. */
  discussLabel: string
}

/** Every status, in the order the person should work through the sections. */
export const STATUS_INFO: Record<AutofixStatus, StatusInfo> = {
  choice: {
    badge: 'Choose', heading: 'Pick an option', needsYou: true,
    hint: 'More than one fix is reasonable. Each option was checked against the issues; picking one applies it.',
    explanationLabel: 'The assistant says', reasonsLabel: 'Why it wasn’t applied automatically',
    discussLabel: 'None of these — discuss in chat',
  },
  review: {
    badge: 'Review', heading: 'Review a proposed change', needsYou: true,
    hint: 'The assistant proposed a change that did not pass every automatic check. Open it to see what it changes, then apply or discard it.',
    explanationLabel: 'The assistant says', reasonsLabel: 'Why it wasn’t applied automatically', discussLabel: 'Discuss in chat',
  },
  input: {
    badge: 'Needs input', heading: 'Needs your input', needsYou: true,
    hint: 'The assistant could not fix these without more information. Answer its questions or discuss them in chat.',
    explanationLabel: 'The assistant says', reasonsLabel: 'Why it wasn’t applied automatically', discussLabel: 'Discuss in chat',
  },
  failed: {
    badge: 'Failed', heading: 'Could not be fixed', needsYou: true,
    hint: 'Something went wrong while working on these. Discuss them in chat or run auto-fix on them again.',
    explanationLabel: 'The assistant says', reasonsLabel: 'Error', discussLabel: 'Discuss in chat',
  },
  fixed: {
    badge: 'Fixed', heading: 'Fixed automatically', needsYou: false,
    hint: 'These passed every check and were applied. Undo them all at once with “Undo automatic fixes”.',
    explanationLabel: 'What was changed', reasonsLabel: '', discussLabel: '',
  },
  resolved: {
    badge: 'Already fixed', heading: 'Already fixed', needsYou: false,
    hint: 'A fix earlier in this run also resolved these.',
    explanationLabel: 'The assistant says', reasonsLabel: 'Why', discussLabel: '',
  },
}

export interface ReportItem { key: number; group: AutofixGroup }
export interface ReportSection { status: AutofixStatus; items: ReportItem[] }

/** The non-empty sections, in STATUS_INFO's order, each item keyed by its position in the run's groups. */
export function reportSections(groups: AutofixGroup[]): ReportSection[] {
  const items = groups.map((group, key) => ({ key, group }))
  return (Object.keys(STATUS_INFO) as AutofixStatus[])
    .map((status) => ({ status, items: items.filter((i) => i.group.status === status) }))
    .filter((s) => s.items.length > 0)
}

/** How many groups were fixed, how many needed the person, and how many of those they have handled. */
export function tally(groups: AutofixGroup[], states: Record<string, ProposalState>) {
  const forYou = groups.filter((g) => STATUS_INFO[g.status].needsYou)
  return {
    fixed: groups.filter((g) => g.status === 'fixed').length,
    forYou: forYou.length,
    handled: forYou.filter((g) => isHandled(g, states)).length,
  }
}

/** The line that names an item: the question it asks when it asks one, else its first issue. */
export function headline(group: AutofixGroup): string {
  if (group.status === 'choice' && group.choices?.[0]) return group.choices[0].question
  if (group.status === 'input' && group.questions?.[0]) return group.questions[0]
  return group.explanations[0] ?? group.reasons?.[0] ?? 'Issue'
}

/** The group's issues as the model has them now. */
export function groupIssues(group: AutofixGroup, issues: ReviewIssue[]): ReviewIssue[] {
  const wanted = new Set(group.issues)
  return issues.filter((i) => wanted.has(i.id))
}

/** The objects these issues affect that are in the model, once each, in the issues' order. */
export function affectedRows(issues: ReviewIssue[], rows: Map<string, Row>): Row[] {
  return [...new Set(issues.flatMap((i) => i.affected_ids))].map((id) => rows.get(id)).filter((r): r is Row => !!r)
}

/** Whether a proposal still waits for the person (not yet decided either way). */
export const isOpenProposal = (state: ProposalState | undefined) => !state || state === 'pending' || state === 'stale'

/** The option the person applied, if any; and whether the choice is closed (every option withdrawn). */
export function choiceOutcome(choice: AutofixChoice, states: Record<string, ProposalState>) {
  return {
    chosen: choice.options.find((o) => states[o.proposal_id] === 'applied'),
    closed: choice.options.every((o) => states[o.proposal_id] === 'dismissed'),
  }
}

/** Whether the person has dealt with a group that needed them: every choice answered or withdrawn, or the
 * proposal decided. Questions and failures are never settled here (they move to the chat). */
export function isHandled(group: AutofixGroup, states: Record<string, ProposalState>): boolean {
  if (group.status === 'review') return !isOpenProposal(group.proposal_id ? states[group.proposal_id] : undefined)
  if (group.status === 'choice') {
    return (group.choices ?? []).every((c) => { const o = choiceOutcome(c, states); return !!o.chosen || o.closed })
  }
  return false
}

/** "RO-1", "RO-1, RO-2" or "RO-1, RO-2 +3". */
export function labelList(rows: Row[], shown = 2): string {
  const names = rows.slice(0, shown).map((r) => r.label || r.id)
  return names.join(', ') + (rows.length > shown ? ` +${rows.length - shown}` : '')
}
