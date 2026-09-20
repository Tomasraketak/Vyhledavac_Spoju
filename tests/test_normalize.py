"""Porovnávání názvů stanic."""

import pytest

from vyhledavac.normalize import best_match, matches_station, normalize, same_place


@pytest.mark.parametrize("a,b", [
    ("Praha", "Prague"), ("Vídeň", "Wien"), ("Mnichov", "München"),
    ("Bratislava", "bratislava"), ("Drážďany", "Dresden"),
])
def test_aliases_match(a, b):
    assert same_place(a, b)


@pytest.mark.parametrize("a,b", [
    ("Praha", "Prachatice"), ("Brno", "Bruntál"), ("Vídeň", "Videnská"),
])
def test_similar_names_do_not_match(a, b):
    assert not same_place(a, b)


def test_station_suffixes_ignored():
    assert matches_station("Břeclav", "Břeclav hl.n.")
    assert matches_station("Wien", "Wien Hauptbahnhof")


def test_best_match_prefers_exact_city():
    options = ["Prachatice", "Praha hl.n.", "Letiště Praha"]
    assert best_match("Praha", options) == 1


def test_best_match_returns_none_when_nothing_fits():
    assert best_match("Lisabon", ["Praha", "Brno"]) is None


def test_normalize_strips_punctuation_and_accents():
    assert normalize("Ústí nad Labem hl. n.") == "usti nad labem hl n"
