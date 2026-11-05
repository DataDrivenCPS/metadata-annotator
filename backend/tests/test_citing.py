"""Linking an uncited created object to the sources that name it (agent/citing.py)."""

import pytest

from workbench.agent.citing import naming, observation_text, tags


@pytest.mark.parametrize("label, text", [
    ("P-101", "Feed pump P101 starts on low level."),
    ("P-101", "Feed pump P 101 starts on low level."),
    ("P101", "Feed pump P-101 starts on low level."),
    ("P-101", "Points: P-101-STS, P-101-CMD"),
    ("AHU 1", "AHU-1 serves the second floor."),
    ("ro-1", "The RO-1 skid"),
])
def test_tags_match_across_spellings(label, text):
    assert tags(label) & tags(text)


@pytest.mark.parametrize("label, text", [
    ("P-101", "P-1010 is a spare."),
    ("TK-2", "TK-20 and TK-21"),
    ("P-101", "page 101"),
])
def test_tags_do_not_match_other_tags(label, text):
    assert not tags(label) & tags(text)


def test_whole_label_is_preferred_to_shared_tags():
    texts = {"obs-a": "Pump P-101 and tank TK-2", "obs-b": "P-101 Feed Pump, duty", "obs-c": "Nothing here"}
    assert naming("P-101 Feed Pump", texts) == ["obs-b"]


def test_most_tags_wins_when_no_text_has_the_whole_label():
    texts = {"obs-a": "P-101 discharges to TK-2.", "obs-b": "P-101 is on the skid.", "obs-c": "TK-2 level"}
    assert naming("P-101 to TK-2 line", texts) == ["obs-a"]
    assert naming("P-101", texts) == ["obs-a", "obs-b"]


def test_nothing_named_gives_nothing():
    texts = {"obs-a": "A drawing with no labels.", "obs-b": ""}
    assert naming("Feed pump", texts) == []
    assert naming("P-9", texts) == []
    assert naming("", texts) == [] and naming(None, texts) == []


def test_record_names_and_values_count_as_text():
    body = {"content": {"name": "LT-101", "metadata": {"Description": "Raw water tank level", "Equipment": "TK-101"}}}
    text = observation_text(body)
    assert naming("TK-101", {"obs-a": text, "obs-b": "LT-102"}) == ["obs-a"]
    assert observation_text(None) == ""
