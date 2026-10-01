"""Document readers and multimodal proposals, with scripted inference."""
import io
import zipfile

import pytest
from PIL import Image

from workbench.documents import document_text, pdf_page, source_context, validate_regions
from workbench.llm import CancelToken, LLMError
from workbench.schemas import SelectionScope, SourceRegion
from workbench.sources import SourceError


def text_pdf(*texts):
    """Small valid PDF with one text-bearing page per string, no external fixture/tool."""
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'']
    pages = []
    for text in texts:
        page_id = len(objects) + 1
        stream_id = page_id + 1
        pages.append(page_id)
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> /Contents {stream_id} 0 R >>'.encode())
        stream = f'BT /F1 14 Tf 72 700 Td ({text}) Tj ET'.encode()
        objects.append(b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream')
    objects[1] = f'<< /Type /Pages /Kids [{" ".join(f"{i} 0 R" for i in pages)}] /Count {len(pages)} >>'.encode()
    data = b'%PDF-1.4\n'
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f'{index} 0 obj\n'.encode() + obj + b'\nendobj\n'
    xref = len(data)
    data += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    data += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    data += f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    return data


def image_data():
    out = io.BytesIO()
    Image.new('RGB', (320, 200), 'white').save(out, format='PNG')
    return out.getvalue()


def test_pdf_upload_text_render_and_page_bounds(sample_project):
    p = sample_project
    data = text_pdf('Pump P-1', 'Tank TK-1')
    source = p.add_source('drawing.pdf', data)
    assert source.kind == 'pdf' and source.page_count == 2
    text, image = pdf_page(data, 2)
    assert 'Tank TK-1' in text
    with Image.open(io.BytesIO(image)) as rendered:
        assert max(rendered.size) <= 2048
    with pytest.raises(SourceError, match='outside'):
        pdf_page(data, 3)
    with pytest.raises(SourceError, match='Could not read the PDF'):
        p.add_source('broken.pdf', b'not a PDF')


def test_word_and_text_upload(sample_project):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Pump P-2</w:t></w:r></w:p></w:body></w:document>')
    assert document_text(stream.getvalue(), 'plant.docx') == 'Pump P-2'
    assert sample_project.add_source('plant.docx', stream.getvalue()).kind == 'document'
    assert sample_project.add_source('notes.md', b'P-2 feeds TK-2.').kind == 'document'
    with pytest.raises(SourceError, match='Word document'):
        document_text(b'broken', 'plant.docx')


def test_pdf_page_selection_and_text_only_context(sample_project):
    p = sample_project
    source = p.add_source('drawing.pdf', text_pdf('Page one pump', 'Page two tank'))
    region = SourceRegion(source_id=source.id, pages=[2])
    client = type('TextModel', (), {'supports_images': False})()
    context, images, evidence = source_context(p, [region], client, CancelToken())
    assert 'Page two tank' in context and 'Page one pump' not in context
    assert images == [] and len(evidence) == 1
    observation = p.observations(source.id)[0]
    assert observation.location.page == 2
    assert 'geometry is unavailable' in context
    source_context(p, [region], client, CancelToken())
    assert len(p.observations(source.id)) == 1  # stable evidence across replies/refreshes
    for pages in ([0], [3], [], [1, 1]):
        with pytest.raises(SourceError):
            validate_regions(p, [SourceRegion(source_id=source.id, pages=pages)])
    big = p.add_source('long.pdf', text_pdf(*['Page'] * 9))
    with pytest.raises(SourceError, match='at most 8'):
        validate_regions(p, [SourceRegion(source_id=big.id)])


def test_image_requires_vision_and_scan_requires_vision(sample_project):
    p = sample_project
    source = p.add_source('plant.png', image_data())
    client = type('TextModel', (), {'supports_images': False})()
    with pytest.raises(LLMError, match='vision-capable'):
        source_context(p, [SourceRegion(source_id=source.id)], client, CancelToken())
    scan = io.BytesIO()
    Image.new('RGB', (50, 50), 'white').save(scan, format='PDF')
    source = p.add_source('scan.pdf', scan.getvalue())
    with pytest.raises(LLMError, match='no readable text'):
        source_context(p, [SourceRegion(source_id=source.id)], client, CancelToken())


def test_image_proposal_keeps_evidence_and_waits_for_apply(sample_project):
    from workbench.agent.correction import run_correction
    from workbench.agent.guidance import SkillGuidance
    from workbench.config import load_settings
    from test_agent import ScriptedLLM

    p = sample_project
    source = p.add_source('plant.png', image_data())
    client = ScriptedLLM([{'action': 'propose', 'explanation': 'Read the tank and its pressure point.',
                          'operations': [
                              {'op': 'create_equipment', 'id': 'new:tk', 'label': 'IMG-TK', 'type': 'watr:Tank'},
                              {'op': 'create_point', 'label': 'IMG-PT', 'equipment': 'new:tk', 'point_kind': 'measurement', 'unit': 'unit:PSI'},
                          ]}])
    client.supports_images = True
    captured = []
    original = client.complete_json
    def complete(*args, **kwargs):
        captured.extend(kwargs.get('images') or [])
        return original(*args, **kwargs)
    client.complete_json = complete
    base = p.head()
    selection = SelectionScope(source_regions=[SourceRegion(source_id=source.id)])
    outcome = run_correction(p, client, SkillGuidance(load_settings().skill_dir), base,
                             selection, 'Build from this image', None, lambda *args: None,
                             CancelToken(), build_from_sources=True)
    assert captured and p.head() == base
    assert outcome.proposal.kind == 'build'
    assert all(operation.evidence for operation in outcome.proposal.operations)
    assert outcome.proposal.selection.source_regions[0].source_id == source.id
    applied = p.apply_proposal(outcome.proposal.id)
    point = next(row for row in p.view(applied.id).points if row.label == 'IMG-PT')
    assert point.equipment.label == 'IMG-TK' and point.evidence
    assert 'label' not in point.locked
