"""Terms copied together with the label a lookup printed beside them still resolve."""

from workbench.operations import OperationList
from workbench.projection import project as project_view
from workbench.vocabulary import S223, expand_term

WATER = str(S223) + "Fluid-Water"


def test_label_beside_a_term_is_ignored_when_the_term_is_real(vocab):
    for value in ("s223:Fluid-Water", "s223:Fluid-Water (Water)", f"{WATER} (Water)", f"<{WATER}> (Water)",
                  "Water [s223:Fluid-Water]", "s223:Fluid-Water - Water [medium]"):
        assert expand_term(vocab, value) == WATER, value


def test_unknown_terms_are_left_alone_so_they_are_still_reported(vocab):
    assert expand_term(vocab, "Influent") == "Influent"
    assert expand_term(vocab, "s223:Nope (Water)") == str(S223) + "Nope (Water)"  # as before: expanded, then reported


def test_a_bare_name_written_with_its_label_is_read_unless_the_label_is_its_own(registry):
    """``Fluid-Water (Water)`` (no prefix) is read as the term once the label is dropped; a real
    label with parentheses, such as Brick's "Laboratory (dry)", is read as written."""
    assert registry.get("watr").named("Fluid-Water (Water)", {"medium"}) == [WATER]
    brick = registry.get("brick")
    dry = brick.named("Laboratory (dry)", {"location"})
    assert len(dry) == 1 and brick.term(dry[0]).label == "Laboratory (dry)"


def test_a_connection_whose_medium_carries_its_label_builds_and_notes_the_reading(workspace):
    p = workspace.create("Labels", "watr")
    cand = p.build_candidate(p.head(), OperationList.validate_python([
        {"op": "create_equipment", "id": "new:p", "label": "P-1", "type": "s223:Pump"},
        {"op": "create_equipment", "id": "new:t", "label": "TK-1", "type": "watr:Tank"},
        {"op": "create_connection", "from_equipment": "new:p", "to_equipment": "new:t", "medium": "Fluid-Water (Water)"},
    ]))
    assert project_view(cand.after, p.vocab).connections[0].medium.iri == WATER  # type: ignore[union-attr]
    assert any("Fluid-Water (Water)" in r for r in cand.ops.readings)  # type: ignore[attr-defined]
