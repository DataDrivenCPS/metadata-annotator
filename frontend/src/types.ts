// Mirrors backend/src/workbench/{projection,schemas}.py

export interface TermRef { iri: string | null; label: string }
export interface EntityRef { id: string; label: string }

export interface EquipmentRow {
  kind: 'equipment'; id: string; iri: string; label: string
  type: TermRef | null; process: TermRef | null; contained_in: EntityRef | null
  point_count: number; locked: string[]; evidence: string[]
  location: EntityRef | null
}
export interface SpaceRow {
  kind: 'space'; id: string; iri: string; label: string
  type: TermRef | null; part_of: EntityRef | null; equipment_count: number; locked: string[]; evidence: string[]
}
export interface PointRow {
  kind: 'point'; id: string; iri: string; label: string
  point_kind: string; point_kind_label: string
  quantity_kind: TermRef | null; unit: TermRef | null; unit_symbol: string
  equipment: EntityRef | null; medium: TermRef | null; substance: TermRef | null
  sensor_type: TermRef | null; locked: string[]; evidence: string[]
  point_type: TermRef | null
}
export interface ConnectionRow {
  kind: 'connection'; id: string; iri: string; label: string
  type: TermRef | null; from_equipment: EntityRef | null; to_equipment: EntityRef | null
  medium: TermRef | null; directed: boolean; locked: string[]; evidence: string[]
  from_point: EntityRef | null; to_point: EntityRef | null
}
export interface ConnectionPointRow {
  kind: 'connection_point'; id: string; iri: string; label: string
  equipment: EntityRef | null; direction: 'inlet' | 'outlet' | 'bidirectional'; medium: TermRef | null
  connection: EntityRef | null; paired_with: EntityRef | null
  maps_to: EntityRef | null; mapped_from: EntityRef | null; locked: string[]; evidence: string[]
}
/** An instance of any other ontology class (a wall, a zone, a system...). */
export interface EntityRow {
  kind: 'entity'; id: string; iri: string; label: string
  type: TermRef | null; relation_count: number; locked: string[]; evidence: string[]
}
/** Any other ontology relation between entities (or from an entity to a vocabulary term). */
export interface RelationshipRow {
  kind: 'relationship'; id: string; iri: string; label: string
  subject: EntityRef; relation: TermRef; object: EntityRef | null; value: TermRef | null
  symmetric: boolean; locked: string[]; evidence: string[]
}
export type Row = EquipmentRow | PointRow | ConnectionRow | ConnectionPointRow | SpaceRow | EntityRow | RelationshipRow

export interface ModelView {
  equipment: EquipmentRow[]; points: PointRow[]; connections: ConnectionRow[]
  connection_points: ConnectionPointRow[]
  spaces: SpaceRow[]
  entities: EntityRow[]; relationships: RelationshipRow[]
  containment: [string, string][]
}

export interface ValidationSummary {
  conforms: boolean; violations: number; warnings: number; suggestions: number; duration_s: number
}
export interface Revision {
  id: string; parent_id: string | null; created_at: string
  author: string; kind: string; summary: string; proposal_id: string | null
  validation: ValidationSummary | null; operation_count?: number
}
export interface ReviewIssue {
  id: string; affected_ids: string[]; category: string
  severity: 'violation' | 'warning' | 'suggestion'; explanation: string
  resolution_state: 'open' | 'resolved' | 'dismissed'; origin: string
  details: { findings?: { focus: string; shape: string | null; path: string | null; message: string; severity: string }[] }
  dismissal?: IssueDismissalRecord | null
}
/** pyshifty's repair witness for an issue, rendered by the backend (the engine's own words). */
export interface IssueRepair {
  blocked: boolean; summary: string[]; missing: string[]; offending: string[]; repair: string; shape: string | null
}
export interface IssueDismissalRecord {
  dismissed_by: 'person' | 'assistant'; reason: string | null; proposal_id: string | null
  revision: string | null; created_at: string
}
export type Family = 's223' | 'brick'
export interface ProjectInfo {
  id: string; name: string; namespace: string; head: string
  profile: string; profile_label: string; family: Family
  can_undo: boolean; can_redo: boolean; updated_at: string; error?: string
}
export interface ModelResponse {
  revision: Revision; head: string; info: ProjectInfo; view: ModelView
  issues: ReviewIssue[]; layout: Record<string, [number, number]>
}

export interface SourceRegion { source_id: string; bbox?: number[] | null; rows?: number[] | null; pages?: number[] | null }
export interface Selection {
  entity_ids: string[]; relationship_ids: string[]; field_ids: string[]; source_regions: SourceRegion[]
}
export const emptySelection = (): Selection => ({ entity_ids: [], relationship_ids: [], field_ids: [], source_regions: [] })

export interface FieldChange { field: string; before: unknown; after: unknown }
export interface EntityChange {
  entity_id: string; entity_kind: string; label: string
  change: 'created' | 'updated' | 'deleted'; fields: FieldChange[]
  in_selection: boolean; overrides_locked: string[]
}
export interface EvidenceRef { kind: string; ref: string; summary: string }
export interface Proposal {
  id: string; base_revision: string; created_at: string; selection: Selection
  instruction: string; operations: Record<string, unknown>[]
  affected_ids: string[]; out_of_scope_ids: string[]; changes: EntityChange[]
  diff: { added: string[]; removed: string[] }; evidence: EvidenceRef[]
  validation: { before: ValidationSummary; after: ValidationSummary; resolved: string[]; introduced: string[] } | null
  explanation: string; notes: string[]; questions: string[]
  status: 'pending' | 'applied' | 'dismissed' | 'stale' | 'failed'
  agent_run_id: string | null; applied_revision: string | null
  kind: 'correction' | 'build'
  build_summary: BuildSummary | null
  parent_proposal_id?: string | null
  conversation?: { role: 'user' | 'assistant'; text: string }[]
  issue_dismissals?: IssueDismissal[]
  /** The repair engine's soundness gate on this change (pyshifty). */
  gate?: { sound: boolean; progress: boolean; fixed: string[]; introduced: string[] } | null
}
export interface IssueDismissal { id: string; explanation: string; severity: string; reason: string }
export interface BuildMapping {
  id: string; token?: string; units?: string | null; count: number; examples: string[]; mapped: boolean
  term: string | null; term_label: string | null; point_kind?: string | null; unit?: string | null
  points?: string[]; process?: string | null
}
export interface BuildSummary {
  title: string; records: number; already_modeled: number
  revised?: boolean; revision_note?: string
  parse: { pattern: string | null; description: string; coverage: number; unparsed_examples: string[]; explanation: string }
  points_created: number; equipment_created: number; mapped_tokens: number; token_count: number
  point_mappings: BuildMapping[]; equipment_mappings: BuildMapping[]; unmapped_records: number
  sources: { id: string; filename: string }[]
}
export interface ProgressEvent { at: string; stage: string; message: string; data: Record<string, unknown> }
export interface AgentRun {
  id: string; kind: string; input_revision: string; selection: Selection | null
  instruction: string; provider: string; model: string; skill_version: string
  source_ids?: string[]
  mode?: 'assist' | 'reply' | 'reconsider' | 'build'
  parent_run_id?: string | null
  conversation?: { role: 'user' | 'assistant'; text: string }[]
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  progress: ProgressEvent[]
  outcome: { proposal_id?: string | null; dismissed_proposal_id?: string | null; questions?: string[]; explanation?: string; steps?: number; input_tokens?: number; output_tokens?: number }
  error: string | null; created_at: string; finished_at: string | null
}
export interface Term { iri: string; label: string; kind?: string; symbol?: string; comment?: string }
export interface Provider {
  name: string; kind: string; model: string; base_url: string | null
  supports_images: boolean; default: boolean; has_key: boolean | null
}
export interface VocabularyStatus {
  name: string; label: string; family: Family; description: string; sources: string[]
  ready: boolean; loading: boolean; error: string | null; terms: number; root: string; missing_imports: string[]
}
export interface Status {
  version: string; skill_version: string; data_dir: string; providers: Provider[]
  vocabulary: VocabularyStatus; vocabularies: VocabularyStatus[]; default_vocabulary: string
}
export interface EntityDetail {
  id: string; iri: string; revision: string; row: Row | null
  types: { iri: string; label: string }[]; turtle: string; locked: string[]
  evidence: { id: string; content: Record<string, unknown>; location: Record<string, unknown> }[]
  issues: ReviewIssue[]
  history: { revision_id: string; field: string; before: string; after: string; origin: string; created_at: string }[]
}

export interface ProviderHealth { ok: boolean; detail: string; model?: string; model_count?: number }

export interface CsvImportConfig {
  layout: 'header_points' | 'row_points' | 'column_points'
  delimiter: string
  header_row: number | null
  excluded_columns: number[]
  name_column: number | null
  metadata_columns: number[]
  first_data_row: number | null
  name_row: number | null
  metadata_rows: number[]
  first_data_column: number
}
export interface Source {
  id: string; kind: 'csv' | 'image' | 'pdf' | 'document' | 'rdf'; filename: string; sha256: string; created_at: string
  import_config: CsvImportConfig | null; status: string; width: number | null; height: number | null
  page_count?: number | null
  observation_count?: number
  modeled_count?: number
}
export interface CsvGrid {
  rows: string[][]; total_rows: number; columns: number; delimiter: string
  config: CsvImportConfig; suggested: CsvImportConfig; reason: string; confirmed: boolean
}
export interface CsvRecord { name: string; metadata: Record<string, string>; location: { kind: string; row?: number; column?: number } }
export interface CsvPreview { total: number; duplicates: Record<string, number>; metadata_keys: string[]; records: CsvRecord[] }
export interface Observation {
  id: string; source_id: string; kind: string; status: string; entity_ids: string[]
  content: { name?: string; metadata?: Record<string, string> } & Record<string, unknown>
  location: { kind: string; row?: number | null; column?: number | null; bbox?: number[] | null }
  entity_id?: string | null
}
