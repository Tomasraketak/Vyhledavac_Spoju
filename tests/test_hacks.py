"""Travel hacky – logika skládání a cenové rozvahy."""

import datetime as dt
from decimal import Decimal

import pytest

from vyhledavac.hacks.base import connects, combine
from vyhledavac.hacks.split_ticket import SplitTicketHack, clean_station_name, is_ticketable
from vyhledavac.hacks.hidden_city import HiddenCityHack
from vyhledavac.models import SearchQuery
from tests.conftest import at, make_journey

MIN_TRANSFER = dt.timedelta(minutes=4)


def q(**kw):
    defaults = dict(origin="Praha", destination="Vídeň", departure=at(8))
    defaults.update(kw)
    return SearchQuery(**defaults)


# ---- navazování úseků -------------------------------------------------------
def test_same_train_is_recognised():
    first = make_journey(dep=at(8), arr=at(10), line="EC 103")
    second = make_journey(dep=at(10), arr=at(12), line="EC 103")
    ok, same = connects(first, second, MIN_TRANSFER)
    assert ok and same


def test_different_trains_two_minutes_apart_are_not_the_same_train():
    """Původní kód bral rozdíl <= 3 minuty jako 'tentýž vlak'.

    Tím slepil dva úplně cizí spoje a nabídl jízdenku, kterou nešlo projet.
    """
    first = make_journey(dep=at(8), arr=at(10), line="EC 103")
    second = make_journey(dep=at(10, 2), arr=at(12), line="RJ 71")
    ok, same = connects(first, second, MIN_TRANSFER)
    assert not same
    assert not ok      # 2 minuty na přestup je pod limitem


def test_valid_transfer_between_different_trains():
    first = make_journey(dep=at(8), arr=at(10), line="EC 103")
    second = make_journey(dep=at(10, 20), arr=at(12), line="RJ 71")
    ok, same = connects(first, second, MIN_TRANSFER)
    assert ok and not same


def test_overnight_wait_is_not_a_transfer():
    first = make_journey(dep=at(8), arr=at(10), line="EC 103")
    second = make_journey(dep=at(20), arr=at(22), line="RJ 71")
    ok, _ = connects(first, second, MIN_TRANSFER)
    assert not ok


def test_backwards_connection_rejected():
    first = make_journey(dep=at(10), arr=at(12), line="EC 103")
    second = make_journey(dep=at(9), arr=at(11), line="RJ 71")
    ok, _ = connects(first, second, MIN_TRANSFER)
    assert not ok


# ---- skládání ---------------------------------------------------------------
def test_combine_sums_prices():
    merged = combine("A + B", [make_journey(price=100), make_journey(price=150)])
    assert merged.price == Decimal("250")


def test_combine_flags_missing_price_instead_of_inventing_one():
    merged = combine("A + B", [make_journey(price=100), make_journey(price=None)])
    assert merged.price == Decimal("100")
    assert any("nepodařilo zjistit cenu" in n for n in merged.notes)


# ---- prahy úspory -----------------------------------------------------------
@pytest.mark.parametrize("total,baseline,expected", [
    (Decimal("900"), Decimal("1000"), True),    # 10 % úspora
    (Decimal("990"), Decimal("1000"), False),   # 1 % nestojí za komplikace
    (Decimal("1100"), Decimal("1000"), False),  # dražší
])
def test_min_saving_threshold(total, baseline, expected):
    assert SplitTicketHack().is_worth_it(total, baseline) is expected


# ---- výběr bodů lomu --------------------------------------------------------
def test_border_points_are_not_offered_as_split_nodes():
    assert not is_ticketable("Breclav statní hranice/Staatsgrenze")
    assert not is_ticketable("CZ/PL - Petrovice u Karviné / Zebrzydowice")
    assert is_ticketable("Břeclav")


def test_platform_suffix_is_stripped():
    assert clean_station_name("Wien Hbf (Bahnsteige 3-12)") == "Wien Hbf"


def test_candidates_exclude_endpoints_and_prefer_border_stations():
    hack = SplitTicketHack()
    journey = make_journey(stops=["Praha hl.n.", "Pardubice hl.n.", "Břeclav", "Wien Hbf"])
    nodes = hack._candidate_nodes(q(), [journey])
    assert "Praha hl.n." not in nodes          # výchozí stanice
    assert nodes[0] == "Břeclav"               # hraniční uzel má přednost
    assert "Pardubice hl.n." in nodes


# ---- celý hack na syntetických datech ---------------------------------------
@pytest.mark.asyncio
async def test_split_ticket_finds_saving_on_same_train():
    """Praha->Vídeň za 1200; lomeno v Břeclavi 300 + 500 = 800."""
    direct = make_journey(provider="ČD", dep=at(8), arr=at(12), price=1200,
                          line="EC 103", stops=["Praha", "Břeclav", "Vídeň"])

    async def fake_search(query):
        if query.destination == "Břeclav":
            return [make_journey(provider="ČD", dep=at(8), arr=at(10), price=300,
                                 line="EC 103", origin="Praha", destination="Břeclav")]
        if query.origin == "Břeclav":
            return [make_journey(provider="ČD", dep=at(10), arr=at(12), price=500,
                                 line="EC 103", origin="Břeclav", destination="Vídeň")]
        return []

    found = await SplitTicketHack().find(q(), [direct], fake_search)
    assert len(found) == 1
    assert found[0].price == Decimal("800")
    assert found[0].is_hack
    assert "Břeclav" in found[0].notes[0]


@pytest.mark.asyncio
async def test_split_ticket_rejects_when_not_cheaper():
    direct = make_journey(provider="ČD", dep=at(8), arr=at(12), price=700,
                          line="EC 103", stops=["Praha", "Břeclav", "Vídeň"])

    async def fake_search(query):
        if query.destination == "Břeclav":
            return [make_journey(dep=at(8), arr=at(10), price=400, line="EC 103")]
        if query.origin == "Břeclav":
            return [make_journey(dep=at(10), arr=at(12), price=400, line="EC 103")]
        return []

    assert await SplitTicketHack().find(q(), [direct], fake_search) == []


@pytest.mark.asyncio
async def test_split_ticket_survives_failing_leg_search():
    direct = make_journey(dep=at(8), arr=at(12), price=1200,
                          stops=["Praha", "Břeclav", "Vídeň"])

    async def boom(query):
        raise RuntimeError("dopravce spadl")

    assert await SplitTicketHack().find(q(), [direct], boom) == []


# ---- skryté město -----------------------------------------------------------
@pytest.mark.asyncio
async def test_hidden_city_requires_stop_at_real_destination():
    """Jízdenka dál je levnější, ale vlak v našem cíli nestaví -> nenabízet."""
    direct = make_journey(price=1000, destination="Brno")

    async def fake_search(query):
        return [make_journey(price=600, destination="Břeclav",
                             stops=["Praha", "Pardubice", "Břeclav"])]

    found = await HiddenCityHack().find(
        q(destination="Brno"), [direct], fake_search
    )
    assert found == []


@pytest.mark.asyncio
async def test_hidden_city_skips_reservation_bound_services():
    """U spojů s povinnou místenkou skryté město nefunguje."""
    direct = make_journey(price=1000, destination="Brno")

    async def fake_search(query):
        return [make_journey(price=500, destination="Břeclav",
                             stops=["Praha", "Brno", "Břeclav"],
                             reservation_required=True)]

    found = await HiddenCityHack().find(q(destination="Brno"), [direct], fake_search)
    assert found == []


@pytest.mark.asyncio
async def test_hidden_city_finds_saving_and_warns():
    direct = make_journey(price=1000, destination="Brno")

    async def fake_search(query):
        return [make_journey(price=500, destination="Břeclav", dep=at(8), arr=at(12),
                             stops=["Praha", "Brno", "Břeclav"])]

    found = await HiddenCityHack().find(q(destination="Brno"), [direct], fake_search)
    assert len(found) == 1
    assert found[0].price == Decimal("500")
    assert any("propadá" in n for n in found[0].notes)


def test_split_nodes_one_station_per_city_and_big_hubs_on_long_trip():
    """Praha → Amsterdam: dřív se lomilo v Bad Schandau a třech zastávkách
    Děčína (pro jízdenku totéž) – teď jedna za město a k tomu velké uzly."""
    hack = SplitTicketHack()
    journey = make_journey(destination="Amsterdam", stops=[
        "Praha hl.n.", "Decin hl.n.", "Decin-Priper", "Decin-Prostredni Zleb", "Bad Schandau",
        "Dresden Hbf", "Leipzig Hbf", "Erfurt Hbf", "Frankfurt(Main)Hbf", "Köln Hbf",
        "Emmerich", "Arnhem Centraal", "Amsterdam Centraal"])
    query = q(destination="Amsterdam")
    nodes = hack._candidate_nodes(query, [journey])
    decin = [n for n in nodes if n.lower().startswith("decin")]
    assert len(decin) == 1
    top = nodes[:6]
    assert {"Bad Schandau", "Decin hl.n."} & set(top)              # hranice CZ|DE (jedna stanice)
    assert {"Emmerich", "Arnhem Centraal"} & set(top)               # hranice DE|NL
    assert {"Frankfurt(Main)Hbf", "Köln Hbf", "Dresden Hbf"} & set(top)   # velká města na trase
    assert "Amsterdam Centraal" not in nodes                         # cílové město


@pytest.mark.asyncio
async def test_split_offered_when_cheaper_than_same_train_even_if_bus_is_cheapest():
    """Lomená jízdenka na vlak (800) se nabídne, i když autobus stojí 300 –
    autobus jede jindy a déle, vlak celý stojí 1200."""
    direct = make_journey(provider="ČD", dep=at(8), arr=at(12), price=1200,
                          line="EC 103", stops=["Praha", "Břeclav", "Vídeň"])
    bus = make_journey(provider="FlixBus", dep=at(7), arr=at(13), price=300, line="FlixBus N1")

    async def fake_search(query):
        if query.destination == "Břeclav":
            return [make_journey(provider="ČD", dep=at(8), arr=at(10), price=300,
                                 line="EC 103", origin="Praha", destination="Břeclav")]
        if query.origin == "Břeclav":
            return [make_journey(provider="ÖBB", dep=at(10), arr=at(12), price=500,
                                 line="EC 103", origin="Břeclav", destination="Vídeň")]
        return []

    found = await SplitTicketHack().find(q(), [direct, bus], fake_search)
    assert [j.price for j in found] == [Decimal("800")]


def test_split_note_without_negative_saving():
    """Kombinace dražší než nejlevnější spoj (autobus jindy) se nabídne bez „úspory −247 Kč“."""
    from vyhledavac.hacks.split_ticket import _dominated, _similar_price

    train = make_journey(provider="PKP", dep=at(8), arr=at(12), price=500, line="EIC 1")
    assert _dominated(at(8), at(12), Decimal(600), [train]) is train
    assert _dominated(at(8), at(11), Decimal(600), [train]) is None      # kombinace přijede dřív
    assert _similar_price(at(8, 10), at(12, 20), [train]) == 500
    assert _similar_price(at(11), at(15), [train]) is None


@pytest.mark.asyncio
async def test_split_note_mentions_saving_against_same_time_journey():
    direct = make_journey(provider="ČD", dep=at(8), arr=at(12), price=1200,
                          line="EC 103", stops=["Praha", "Břeclav", "Vídeň"])
    bus = make_journey(provider="FlixBus", dep=at(6), arr=at(13), price=300, line="FlixBus N1")

    async def fake_search(query):
        if query.destination == "Břeclav":
            return [make_journey(provider="ČD", dep=at(8), arr=at(10), price=300,
                                 line="EC 103", origin="Praha", destination="Břeclav")]
        if query.origin == "Břeclav":
            return [make_journey(provider="ÖBB", dep=at(10), arr=at(12), price=500,
                                 line="EC 103", origin="Břeclav", destination="Vídeň")]
        return []

    (found,) = await SplitTicketHack().find(q(), [direct, bus], fake_search)
    assert "úspora 400 Kč proti spoji ve stejný čas" in found.notes[0]
    assert "-" not in found.notes[0].split("úspora")[1]
