from prompts.report import name_mismatch, render_evidence


def test_name_check_ignores_case_accents_and_a_trailing_parenthetical():
    assert not name_mismatch("Thomas MÜLLER", "Thomas Müller")
    assert not name_mismatch("Nuri Sahin", "Nuri Şahin")
    assert not name_mismatch("Thomas Müller", "Thomas_Müller_(footballer,_born_1989)")
    assert not name_mismatch("Max Eberl", "Max Eberl (footballer)")


def test_name_check_catches_a_mismatch():
    assert name_mismatch("Takuma ano", "Takuma Asano")
    assert name_mismatch("Takuma Asano", "Asano Takuma")
    assert name_mismatch("Max Eberl", "Max Eberl (footballer) Jr")


def test_render_evidence():
    stint = {"predicate": "played_for", "object_name": "Test Club A", "start_year": 2010, "end_year": 2012,
             "apps": 5, "source": "dewiki"}
    assert render_evidence(stint) == "Test Club A 2010–2012 5 apps (dewiki)"
    open_ended = {**stint, "end_year": None, "apps": None, "source": "wikidata"}
    assert render_evidence(open_ended) == "Test Club A 2010– (wikidata)"
    nationality = {"predicate": "nationality", "object_name": "Test Country", "start_year": None,
                   "end_year": None, "apps": None, "source": "wikidata"}
    assert render_evidence(nationality) == "nationality Test Country (wikidata)"
