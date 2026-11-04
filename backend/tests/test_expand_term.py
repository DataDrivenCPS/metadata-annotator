"""Terms copied together with the label a lookup printed beside them still resolve."""

from types import SimpleNamespace

from workbench.vocabulary import S223, expand_term

WATER = str(S223) + "Fluid-Water"


def vocab():
    return SimpleNamespace(namespaces={}, term=lambda iri: object() if iri == WATER else None)


def test_label_suffix_or_prefix_is_ignored_when_the_term_is_real():
    v = vocab()
    for value in ("s223:Fluid-Water", "s223:Fluid-Water (Water)", f"{WATER} (Water)",
                  f"<{WATER}> (Water)", "Water [s223:Fluid-Water]"):
        assert expand_term(v, value) == WATER, value


def test_unknown_terms_are_left_alone_so_they_are_still_reported():
    v = vocab()
    assert expand_term(v, "Influent") == "Influent"
    assert expand_term(v, "s223:Nope (Water)") == str(S223) + "Nope (Water)"  # as before: expanded, then reported
