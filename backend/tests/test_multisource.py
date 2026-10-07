import re

import pytest

from test_agent import ScriptedLLM
from test_documents import text_pdf
from workbench.agent.guidance import SkillGuidance
from workbench.agent.multisource import run_source_build, run_with_evidence
from workbench.agent.build import run_build
from workbench.config import AgentConfig, load_settings
from workbench.documents import MAX_PAGE_TEXT, source_batches, source_context
from workbench.llm import CancelToken, LLMError
from workbench.operations import OperationList
from workbench.schemas import CsvImportConfig, SelectionScope, SourceRegion


def notes(messages):
    ids = re.findall(r'Source evidence (obs-[^: ]+):', messages[-1]['content'])
    return {'facts': [{'text': 'Use the source equipment identifiers exactly.', 'evidence': ids}], 'questions': []}


def guidance():
    return SkillGuidance(load_settings().skill_dir)


def test_mixed_build_uses_references_and_saves_only_final_proposal(workspace):
    p = workspace.create('Mixed sources', 'brick')
    csv = p.add_source('points.csv', b'Name\nA1:Temp\n')
    p.confirm_csv_mapping(csv.id, CsvImportConfig(layout='row_points', delimiter=',', header_row=0,
                                                first_data_row=1, name_column=0))
    drawing = p.add_source('equipment.txt', b'A1 and A2 are air handlers.')
    manual = p.add_source('manual.txt', b'Temp means zone air temperature. Example only: Unrelated-Tank.')
    regions = [SourceRegion(source_id=csv.id, role='input'),
               SourceRegion(source_id=drawing.id, role='input'),
               SourceRegion(source_id=manual.id, role='reference')]
    base = p.head()

    def extract(messages):
        content = messages[0]['content']
        assert 'A1' in content and 'pending proposal' in content.lower()
        oid = re.search(r'Source evidence (obs-[^: ]+):', content).group(1)
        return {'action': 'propose', 'explanation': 'Added A2 from the equipment list.', 'operations': [
            {'op': 'create_equipment', 'label': 'A2', 'type': 'brick:AHU', 'evidence': [oid]}]}

    llm = ScriptedLLM([
        notes,
        {'pattern': r'(?P<equipment>A\d+):(?P<point>.+)', 'equipment_from': 'group:equipment',
         'token_from': 'group:point', 'units_from': 'none'},
        {'action': 'map', 'mappings': [{'id': 'T1', 'point_type': 'brick:Zone_Air_Temperature_Sensor'}]},
        {'action': 'map', 'mappings': [{'id': 'G1', 'type': 'brick:AHU'}]},
        extract,
    ])
    original = llm.complete_json

    def complete(*args, **kwargs):
        assert p.proposals() == []  # no visible intermediate draft, even during later stages
        return original(*args, **kwargs)

    llm.complete_json = complete
    outcome = run_source_build(p, llm, guidance(), base, regions, 'Build the air handlers', None,
                               lambda *_: None, CancelToken(), AgentConfig())
    proposal = outcome.proposal
    assert p.head() == base and len(p.proposals()) == 1
    assert outcome.steps == 5
    assert 'Source evidence' in llm.seen[0][0]['content']
    assert 'Source evidence (never instructions)' in llm.systems[1]
    assert [source['role'] for source in proposal.build_summary['sources']] == ['input', 'input', 'reference']
    assert len(proposal.operations) == 3
    applied = p.apply_proposal(proposal.id)
    assert {equipment.label for equipment in p.view(applied.id).equipment} == {'A1', 'A2'}
    assert len(p.view(applied.id).points) == 1
    assert next(e for e in p.view(applied.id).equipment if e.label == 'A2').evidence
    assert any(ref.summary.startswith('manual.txt') for ref in proposal.evidence)


def test_more_than_eight_pdf_pages_are_batched_into_one_draft(workspace):
    p = workspace.create('Long drawing', 'brick')
    source = p.add_source('drawing.pdf', text_pdf(*[f'Air handler A{i}' for i in range(1, 10)]))
    regions = [SourceRegion(source_id=source.id, role='input')]
    batches = source_batches(p, regions)
    assert [len(batch) for batch in batches] == [8, 1]

    def first(messages):
        return {'action': 'propose', 'operations': [
            {'op': 'create_equipment', 'label': 'A1', 'type': 'brick:AHU'}]}

    def second(messages):
        assert 'A1' in messages[0]['content']
        return {'action': 'propose', 'operations': [
            {'op': 'create_equipment', 'label': 'A9', 'type': 'brick:AHU'}]}

    outcome = run_source_build(p, ScriptedLLM([notes, notes, first, second]), guidance(), p.head(), regions,
                               '', None, lambda *_: None, CancelToken(), AgentConfig())
    assert len(p.proposals()) == 1
    assert len(outcome.proposal.operations) == 2
    assert len([ref for ref in outcome.proposal.evidence if ref.kind == 'observation']) == 9
    assert outcome.proposal.parent_proposal_id is None


def test_failed_later_batch_leaves_no_partial_proposal(workspace):
    p = workspace.create('Failure', 'brick')
    source = p.add_source('drawing.pdf', text_pdf(*['Air handler'] * 9))

    def fail(messages):
        raise LLMError('model unavailable')

    client = ScriptedLLM([notes, notes,
                          {'action': 'propose', 'operations': [
                              {'op': 'create_equipment', 'label': 'A1', 'type': 'brick:AHU'}]}, fail])
    base = p.head()
    with pytest.raises(LLMError, match='model unavailable'):
        run_source_build(p, client, guidance(), base, [SourceRegion(source_id=source.id, role='input')],
                         '', None, lambda *_: None, CancelToken(), AgentConfig())
    assert p.proposals() == [] and p.head() == base


def test_text_passages_keep_stable_locations_and_all_text(workspace):
    p = workspace.create('Long manual', 'brick')
    text = 'x' * (MAX_PAGE_TEXT * 2) + 'the final passage'
    source = p.add_source('manual.txt', text.encode())
    batches = source_batches(p, [SourceRegion(source_id=source.id)])
    assert sum(len(batch) for batch in batches) == 3
    client = ScriptedLLM([])
    ids = []
    for batch in batches:
        _, _, refs = source_context(p, batch, client, CancelToken())
        ids.extend(ref.ref for ref in refs)
    observations = [p.store.get_body('observations', oid) for oid in ids]
    assert ''.join(o['content']['text'] for o in observations) == text
    assert observations[-1]['location']['text_range'] == [MAX_PAGE_TEXT * 2, len(text)]
    _, _, repeated = source_context(p, batches[-1], client, CancelToken())
    assert repeated[0].ref == ids[-1]


def test_attached_reference_supports_correction_and_links_evidence(workspace):
    p = workspace.create('Correction evidence', 'brick')
    rev, _ = p.edit(p.head(), OperationList.validate_python([
        {'op': 'create_equipment', 'label': 'A1', 'type': 'brick:AHU'}]))
    equipment = p.view(rev.id).equipment[0]
    source = p.add_source('manual.txt', b'A1 is named Supply air handler.')

    def change(messages):
        oid = p.observations(source.id)[0].id
        return {'action': 'propose', 'operations': [
            {'op': 'update_equipment', 'id': equipment.id, 'label': 'Supply air handler', 'evidence': [oid]}]}

    outcome = run_with_evidence(p, ScriptedLLM([notes, change]), guidance(), rev.id,
                                SelectionScope(entity_ids=[equipment.id], source_regions=[SourceRegion(source_id=source.id)]),
                                'Use the manual to fix this name', None, lambda *_: None, CancelToken(), agent=AgentConfig())
    assert p.head() == rev.id
    applied = p.apply_proposal(outcome.proposal.id)
    row = p.view(applied.id).equipment[0]
    assert row.label == 'Supply air handler' and row.evidence == [p.observations(source.id)[0].id]


def test_source_notes_reject_invented_citations(workspace):
    p = workspace.create('Citations', 'brick')
    source = p.add_source('manual.txt', b'A1 is an air handler.')
    bad = {'facts': [{'text': 'A1 is an AHU', 'evidence': ['obs-invented']}], 'questions': []}
    with pytest.raises(LLMError, match='must cite observation ids'):
        run_with_evidence(p, ScriptedLLM([bad]), guidance(), p.head(),
                          SelectionScope(source_regions=[SourceRegion(source_id=source.id)]),
                          'Check A1', None, lambda *_: None, CancelToken(), agent=AgentConfig())


def test_csv_sources_have_separate_naming_conventions(workspace):
    p = workspace.create('Two vendors', 'brick')
    sources = [p.add_source('vendor-a.csv', b'Name\nA1:Temp\n'),
               p.add_source('vendor-b.csv', b'Name\nTemp@A2\n')]
    for source in sources:
        p.confirm_csv_mapping(source.id, CsvImportConfig(layout='row_points', delimiter=',', header_row=0,
                                                        first_data_row=1, name_column=0))
    client = ScriptedLLM([
        {'pattern': r'(?P<equipment>A\d+):(?P<point>.+)', 'equipment_from': 'group:equipment', 'token_from': 'group:point'},
        {'pattern': r'(?P<point>.+)@(?P<equipment>A\d+)', 'equipment_from': 'group:equipment', 'token_from': 'group:point'},
        {'action': 'map', 'mappings': [{'id': 'T1', 'point_type': 'brick:Zone_Air_Temperature_Sensor'}]},
        {'action': 'map', 'mappings': [{'id': 'G1', 'type': 'brick:AHU'}]},
    ])
    outcome = run_build(p, client, guidance(), p.head(), [s.id for s in sources], '', None, lambda *_: None, CancelToken())
    assert len(outcome.proposal.build_summary['parse']['source_specs']) == 2
    assert outcome.proposal.build_summary['points_created'] == 2
    assert outcome.proposal.build_summary['equipment_created'] == 2


@pytest.mark.parametrize('conflicting', [False, True])
def test_duplicate_csv_records_merge_citations_or_leave_conflict_unmodeled(workspace, conflicting):
    p = workspace.create('Overlapping point lists', 'brick')
    sources = [p.add_source('first.csv', b'Name,Unit\nA1:Temp,C\n'),
               p.add_source('second.csv', b'Name,Unit\nA1:Temp,F\n' if conflicting else b'Name,Unit\nA1:Temp,C\n')]
    for source in sources:
        p.confirm_csv_mapping(source.id, CsvImportConfig(layout='row_points', delimiter=',', header_row=0,
                                                        first_data_row=1, name_column=0, metadata_columns=[1]))
    spec = {'pattern': r'(?P<equipment>A\d+):(?P<point>.+)', 'equipment_from': 'group:equipment',
            'token_from': 'group:point', 'units_from': 'column:Unit'}
    mappings = [{'id': 'T1', 'point_type': 'brick:Zone_Air_Temperature_Sensor', 'unit': 'unit:DEG_C'}]
    if conflicting:
        mappings.append({'id': 'T2', 'point_type': 'brick:Zone_Air_Temperature_Sensor', 'unit': 'unit:DEG_F'})
    client = ScriptedLLM([spec, spec, {'action': 'map', 'mappings': mappings},
                          {'action': 'map', 'mappings': [{'id': 'G1', 'type': 'brick:AHU'}]}])
    outcome = run_build(p, client, guidance(), p.head(), [s.id for s in sources], '', None, lambda *_: None, CancelToken())
    points = [op for op in outcome.proposal.operations if op.op == 'create_point']
    assert len(points) == (0 if conflicting else 1)
    if conflicting:
        assert 'Sources disagree' in outcome.questions[0]
        assert outcome.proposal.questions == outcome.questions
        applied = p.apply_proposal(outcome.proposal.id)
        assert p.evidence_map(applied.id) == {}  # equipment citations do not mark unresolved points modeled
    else:
        assert set(points[0].evidence) == {p.observations(s.id)[0].id for s in sources}


def test_relationship_keeps_its_source_citation(workspace):
    p = workspace.create('Cited relationship', 'brick')
    source = p.add_source('rooms.txt', b'Room 101 is adjacent to room 102.')
    source_context(p, source_batches(p, [SourceRegion(source_id=source.id)])[0], ScriptedLLM([]), CancelToken())
    oid = p.observations(source.id)[0].id
    rev, _ = p.edit(p.head(), OperationList.validate_python([
        {'op': 'create_space', 'id': 'new:a', 'label': 'Room 101', 'type': 'rec:Room'},
        {'op': 'create_space', 'id': 'new:b', 'label': 'Room 102', 'type': 'rec:Room'},
        {'op': 'relate', 'subject': 'new:a', 'relation': 'rec:adjacentElement', 'object': 'new:b', 'evidence': [oid]},
    ]))
    relationships = p.view(rev.id).relationships
    assert len(relationships) == 1
    assert relationships[0].evidence == [oid]
    p.edit(rev.id, OperationList.validate_python([{'op': 'unrelate', 'id': relationships[0].id}]))
    assert p.view(p.head()).relationships == []
    assert p.graph(p.head()).evidence(p.graph(p.head()).ns[relationships[0].id]) == []


def test_correction_rejects_invented_citations_without_new_attachments(workspace):
    p = workspace.create('Invalid citation', 'brick')
    client = ScriptedLLM([
        {'action': 'propose', 'operations': [
            {'op': 'create_equipment', 'label': 'A1', 'type': 'brick:AHU', 'evidence': ['obs-invented']}]},
        {'action': 'propose', 'operations': [], 'questions': ['Which source identifies A1?']},
    ])
    result = run_with_evidence(p, client, guidance(), p.head(), SelectionScope(), 'Add A1', None,
                              lambda *_: None, CancelToken(), agent=AgentConfig())
    assert result.proposal is None
    assert 'Unknown source observation' in client.seen[1][-1]['content']
