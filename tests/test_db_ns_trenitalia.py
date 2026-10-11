import datetime as dt
import gzip
import json
from decimal import Decimal
from pathlib import Path

import pytest

from vyhledavac.errors import StationNotFoundError
from vyhledavac.models import DEFAULT_TZ, FareStatus, Mode, Passenger, SearchQuery
from vyhledavac.providers import db, ns, trenitalia

DATA = Path(__file__).parent / "data"


def _json(name):
    return json.load(gzip.open(DATA / name, "rt", encoding="utf-8"))


def at(h, m=0):
    return dt.datetime(2026, 10, 12, h, m, tzinfo=DEFAULT_TZ)


# ---- Trenitalia -------------------------------------------------------------------
def test_trenitalia_domestic_prices_and_changes():
    data = _json("trenitalia_solutions.json.gz")
    provider = trenitalia.TrenitaliaProvider()
    journeys = [provider.to_journey(s["solution"], international=False) for s in data["domestic"]]
    fr = journeys[0]
    assert fr.legs[0].line == "FR 9466"
    assert fr.fare_status is FareStatus.PRICED and fr.fares[0].amount == Decimal("99.0")
    change = next(j for j in journeys if len(j.legs) == 2)
    assert [l.line for l in change.legs] == ["RE 3997", "FR 9623"]


def test_trenitalia_international_price_is_not_trusted():
    # Benátky → Vídeň: orientační cena nebo jen italská část trasy (36 € za 3 osoby)
    data = _json("trenitalia_solutions.json.gz")
    provider = trenitalia.TrenitaliaProvider()
    for item in data["international"]:
        journey = provider.to_journey(item["solution"], international=True)
        assert journey.price is None and journey.raw.get("indicative_eur")


def test_trenitalia_location_prefers_city_group():
    items = [{"id": 830002593, "name": "Venezia S. Lucia", "multistation": False},
             {"id": 830002998, "name": "Venezia ( Tutte Le Stazioni )", "multistation": True}]
    assert trenitalia.pick_location("Venezia", items)["id"] == 830002998
    assert trenitalia.pick_location("Venezia S. Lucia", items)["id"] == 830002593


@pytest.mark.asyncio
async def test_trenitalia_skips_without_italy():
    with pytest.raises(StationNotFoundError):
        await trenitalia.TrenitaliaProvider().search(
            SearchQuery(origin="Praha", destination="Wien", departure=at(8)))


# ---- NS ---------------------------------------------------------------------------
def test_ns_domestic_fare_for_group():
    data = _json("ns_trips.json.gz")
    query = SearchQuery(origin="Amsterdam", destination="Utrecht", departure=at(8),
                        passengers=(Passenger(age=30), Passenger(age=8), Passenger(age=2)))
    journey = ns.to_journey(data["domestic"][0], query)
    assert journey.legs[0].line.startswith("IC") and journey.legs[0].mode is Mode.TRAIN
    # 10 € dospělý + 2,50 € Railrunner (8 let) + 0 € (2 roky)
    assert Decimal(journey.raw["eur"]) == Decimal("12.50") and "Railrunner" in journey.raw["tariff"]
    assert len(journey.legs[0].stops) >= 2


def test_ns_international_has_no_price():
    data = _json("ns_trips.json.gz")
    query = SearchQuery(origin="Amsterdam", destination="Berlin Hbf", departure=at(8))
    journey = ns.to_journey(data["international"][0], query)
    assert not journey.raw.get("eur") and "NS International" in journey.notes[0]
    assert journey.legs[-1].destination == "Berlin Hbf"


def test_ns_reads_key_from_planner_page():
    page = 'window.journeyplannerConfiguration = { nsAppInternalProductKey: "0123456789abcdef0123456789abcdef", }'
    assert ns.parse_key(page) == "0123456789abcdef0123456789abcdef"


# ---- DB ---------------------------------------------------------------------------
def test_db_travellers_by_age():
    kinds = [t["typ"] for t in db.travellers((Passenger(age=3), Passenger(age=10), Passenger(age=20),
                                              Passenger(age=40), Passenger(age=70)))]
    assert kinds == ["KLEINKIND", "KIND", "JUGENDLICHER", "ERWACHSENER", "SENIOR"]


def test_db_connection_to_journey():
    raw = {"angebotsPreis": {"betrag": 29.99, "waehrung": "EUR"}, "verbindungsAbschnitte": [
        {"abfahrtsZeitpunkt": "2026-10-12T08:00:00", "ankunftsZeitpunkt": "2026-10-12T11:30:00",
         "abfahrtsOrt": "Praha hl.n.", "ankunftsOrt": "Regensburg Hbf",
         "verkehrsmittel": {"name": "ALX 354", "produktGattung": "REGIONAL", "typ": "PUBLICTRANSPORT"},
         "halte": [{"name": "Praha hl.n.", "abfahrtsZeitpunkt": "2026-10-12T08:00:00"},
                   {"name": "Plzeň hl.n.", "ankunftsZeitpunkt": "2026-10-12T09:30:00",
                    "abfahrtsZeitpunkt": "2026-10-12T09:32:00"},
                   {"name": "Regensburg Hbf", "ankunftsZeitpunkt": "2026-10-12T11:30:00"}]},
        {"abfahrtsZeitpunkt": "2026-10-12T11:30:00", "ankunftsZeitpunkt": "2026-10-12T11:35:00",
         "abfahrtsOrt": "Regensburg Hbf", "ankunftsOrt": "Regensburg Hbf", "verkehrsmittel": {"typ": "WALK"}},
        {"abfahrtsZeitpunkt": "2026-10-12T11:45:00", "ankunftsZeitpunkt": "2026-10-12T13:15:00",
         "abfahrtsOrt": "Regensburg Hbf", "ankunftsOrt": "München Hbf",
         "verkehrsmittel": {"name": "RE 4012", "produktGattung": "REGIONAL", "typ": "PUBLICTRANSPORT"}}]}
    journey = db.DbProvider().to_journey(raw)
    assert [l.mode for l in journey.legs] == [Mode.TRAIN, Mode.WALK, Mode.TRAIN]
    assert journey.legs[0].stops[1].name == "Plzeň hl.n." and journey.departure == at(8)
    assert journey.fares[0].amount == Decimal("29.99")


@pytest.mark.asyncio
async def test_db_skips_routes_away_from_germany():
    with pytest.raises(StationNotFoundError):
        await db.DbProvider().search(SearchQuery(origin="Praha", destination="Budapest", departure=at(8)))
