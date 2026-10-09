"""Databáze zastávek – našeptávač a kraj/IDS/zóna pro jména od dopravců.

Testy běží na skutečném datovém souboru (vyhledavac/data/stations.json.gz),
takže zároveň hlídají, že sestavení dat nic podstatného nerozbilo.
"""

from __future__ import annotations

import pytest

from vyhledavac import stations
from vyhledavac.stations import StationDB


@pytest.fixture(scope="module")
def db():
    database = stations.database()
    assert database is not None, "chybí vyhledavac/data/stations.json.gz"
    return database


def names(places):
    return [p.name for p in places]


# ---- našeptávač ---------------------------------------------------------------
def test_big_city_comes_first(db):
    first = db.suggest("Pra", 5)[0]
    assert first.name == "Praha" and first.kind == "c"


def test_importance_beats_alphabet(db):
    """Praha je důležitější než vlaková zastávka Pražák na Šumavě."""
    found = names(db.suggest("Pra", 2000))
    assert found[0] == "Praha" and "Pražák" in found[50:]
    assert db.suggest("Pražák", 3)[0].name == "Pražák"


def test_without_diacritics_and_case(db):
    assert db.suggest("plzen", 1)[0].name == "Plzeň"
    assert db.suggest("USTI NAD L", 1)[0].name == "Ústí nad Labem"


def test_words_in_any_order(db):
    assert "Brno hl. n." in names(db.suggest("hl brno", 10))


def test_abbreviated_station_found_by_full_name(db):
    """CIS píše 'Brno-Kr. Pole', lidé 'Královo Pole'."""
    assert "Brno-Kr. Pole" in names(db.suggest("brno kralovo pole", 10))


def test_cities_more_important_than_villages(db):
    order = names(db.suggest("O", 10))
    assert order.index("Ostrava") < order.index("Olomouc")


def test_foreign_destinations_stay_available(db):
    assert db.suggest("Vid", 1)[0].name == "Vídeň"
    assert db.suggest("Vid", 1)[0].country == "Rakousko"


def test_empty_query():
    assert stations.suggest("   ") == []


# ---- kraj, IDS, zóny ------------------------------------------------------------
@pytest.mark.parametrize("name, kraj, ids, zone", [
    ("Praha hl.n.", "Hlavní město Praha", "PID", "P"),
    ("Brno hl.n.", "Jihomoravský kraj", "IDS JMK", "100"),
    ("Brno, ÚAN Zvonařka", "Jihomoravský kraj", "IDS JMK", "100"),
    ("Beroun", "Středočeský kraj", "PID", "4"),
    ("Pražák", "Jihočeský kraj", "IDESKA", "723"),
    ("České Budějovice", "Jihočeský kraj", "IDESKA", "100"),
    ("Zliv", "Jihočeský kraj", "IDESKA", "107"),
    ("Ostrava-Svinov", "Moravskoslezský kraj", "ODIS", None),
    ("Liberec", "Liberecký kraj", "IDOL", None),
])
def test_place_info(name, kraj, ids, zone):
    info = stations.info(name)
    assert info is not None, name
    assert info["kraj"] == kraj and info["ids"] == ids
    if zone:
        assert zone in info["zones"]


def test_provider_spellings_resolve():
    assert stations.info("Praha (ÚAN Florenc)")["name"] == "Praha, ÚAN Florenc"
    assert stations.info("Praha-Smíchov")["zones"][0] == "P"
    # neznámé upřesnění za názvem obce – aspoň obec
    assert stations.info("Olomouc, nádraží XYZ")["name"] == "Olomouc"


def test_foreign_has_country_not_kraj():
    info = stations.info("Wien Hbf")
    assert info["country"] == "Rakousko" and info["kraj"] is None


def test_unknown_place():
    assert stations.info("Xyzzy Qwerty") is None
    assert stations.info("") is None


# ---- formát dat -----------------------------------------------------------------
def test_from_json_minimal():
    data = {
        "kraje": ["Kraj A"], "ids": [["IDS A", "Systém A"]], "okresy": [["Okres A", 0]],
        "places": [["Malá", "b", "Velká", 0, 0, 0, "", 5, ""],
                   ["Velká", "c", "", 0, 0, 0, "1,2", 500, "velka obec"]],
    }
    small = StationDB.from_json(data)
    hit = small.suggest("vel", 5)[0]
    assert hit.name == "Velká" and hit.zones == ("1", "2") and hit.ids_name == "Systém A"
    assert small.info("velka obec").name == "Velká"
