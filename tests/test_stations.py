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


@pytest.mark.parametrize("name, expected, kraj", [
    ("Veselí n.Lužnicí", "Veselí nad Lužnicí", "Jihočeský kraj"),     # dřív „Veselí“ v Pardubickém
    ("Náměšť n.Oslavou", "Náměšť nad Oslavou", "Kraj Vysočina"),
    ("Kostelec n.Orl.", "Kostelec nad Orlicí", "Královéhradecký kraj"),
    ("Týn n.Vlt.", "Týn nad Vltavou", "Jihočeský kraj"),
])
def test_info_understands_timetable_abbreviations(name, expected, kraj):
    place = stations.place(name)
    assert place.name == expected and place.kraj == kraj


# ---- zahraniční města (data/cities.json.gz) ------------------------------------
@pytest.mark.parametrize("typed, expected, code", [
    ("Mni", "Mnichov", "DE"), ("Lub", "Lublaň", "SI"), ("kodan", "Kodaň", "DK"),
    ("munchen", "Mnichov", "DE"), ("Lisa", "Lisabon", "PT"), ("Sofi", "Sofie", "BG"),
    ("osl", "Oslo", "NO"), ("Lon", "Londýn", "GB"), ("Bělehr", "Bělehrad", "RS"),
])
def test_foreign_cities_in_czech(db, typed, expected, code):
    first = db.suggest(typed, 5)[0]
    assert (first.name, first.code) == (expected, code)


def test_foreign_city_does_not_hide_czech_town(db):
    found = names(db.suggest("Ber", 6))
    assert "Beroun" in found and found.index("Beroun") < found.index("Bern")


def test_provider_name_translates_exonyms():
    assert stations.provider_name("Lublaň") == "Ljubljana"
    assert stations.provider_name("Curych") == "Zürich"
    assert stations.provider_name("Kodaň") == "København"
    assert stations.provider_name("Vídeň") == "Wien"            # ne obec Vídeň u Žďáru
    assert stations.provider_name("Praha") == "Praha"
    assert stations.provider_name("Mnichovo Hradiště") == "Mnichovo Hradiště"
    assert stations.provider_name("Ljubljana") == "Ljubljana"


def test_country_of_any_european_city():
    from vyhledavac import geo

    assert [geo.country_of(n) for n in ("Lublaň", "Tallinn", "Porto", "Most", "Kodaň")] == \
        ["SI", "EE", "PT", "CZ", "DK"]
    assert not stations.is_domestic("Praha", "Vídeň")
