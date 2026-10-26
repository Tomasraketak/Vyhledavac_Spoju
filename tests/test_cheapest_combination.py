"""Nejlevnější skladba jízdenek v hacku ``idos_complete`` – varianty, dílčí
nabídky e-shopu IDOS a ÖBB, doplnění chybějícího kusu tarifem IDS."""

import datetime as dt
from decimal import Decimal

import pytest

from vyhledavac import ticketplan
from vyhledavac.hacks import idos_complete as ic
from vyhledavac.models import Fare, FareStatus, Journey, Leg, Mode, SearchQuery
from vyhledavac.normalize import normalize

from conftest import at
from test_ticketplan import GROUP, bus_300, cut, r_634, sp_1666, stop, ticket


def rj_512():
    stops = [stop("Plzeň hl.n.", 12, 4, start=True), stop("Stříbro", 12, 30),
             stop("Mariánské Lázně", 13, 5, end=True)]
    return Leg("Plzeň hl.n.", "Mariánské Lázně", at(12, 4), at(13, 5), Mode.TRAIN, "České dráhy, a.s.",
               "rj 512 Ostravan", stops=stops)


def _hack(seen=None, routes=(), base=()):
    hack = ic.IdosCompleteHack()
    hack._seen = dict(seen or {})
    hack._routes = list(routes)
    hack._base = list(base)
    return hack


@pytest.mark.asyncio
async def test_variant_on_regional_train_gets_ids_ticket_across_pieces():
    """Skupina Třeboň → Mariánské Lázně: varianta se spěšným vlakem z Plzně
    vyjde 639 Kč (IDESKA + síťová IDPK), ne 345 + 294 + 602 Kč."""
    query = SearchQuery(origin="Třeboň", destination="Mariánské Lázně", departure=at(8), passengers=GROUP)
    train = r_634()
    head = [ticket("IDESKA", [bus_300(), *cut(train, 0, 2)], 345), ticket("IDPK", cut(train, 2, 5), 294)]
    sjt_rj = ticket("Arriva", [rj_512()], 602, "SJT (Systém jednotného tarifu)")
    sjt_sp = ticket("Arriva", [sp_1666()], 602, "SJT (Systém jednotného tarifu)")
    key = (normalize(ic._full("Plzeň hl.n.")), normalize(ic._full("Mariánské Lázně")), False, False)
    hack = _hack({key: [sjt_rj, sjt_sp]})
    variants = await hack._variants(query, [*head, sjt_rj])
    prices = sorted(v.price for v in variants)
    assert prices[0] == Decimal(639)
    best = min(variants, key=lambda v: v.price)
    assert [s.provider for s in best.segments] == ["IDESKA", "IDPK"]
    assert best.arrival == at(14, 14)


@pytest.mark.asyncio
async def test_eshop_offer_for_part_of_other_route_is_used():
    """Praha → Paříž nočním vlakem: ÖBB chce za Praha → Offenburg 7212 Kč, e-shop
    IDOS za tentýž vlak (u jiného spoje z IDOS, jen na část trasy) 1394 Kč."""
    query = SearchQuery(origin="Praha", destination="Paříž", departure=at(8))
    night = Leg("Praha hl.n.", "Offenburg", at(18, 31), at(23, 46), Mode.TRAIN, "ČD", "EC 458 Canopus",
                stops=[stop("Praha hl.n.", 18, 31, start=True), stop("Offenburg", 23, 46, end=True)])
    tgv = Leg("Offenburg", "Paris Est", at(23, 55), at(23, 59), Mode.TRAIN, "SNCF", "TGV 9530",
              stops=[stop("Offenburg", 23, 55, start=True), stop("Paris Est", 23, 59, end=True)])
    segments = [ticket("ÖBB", [night], 7212), ticket("SNCF", [tgv], 1983)]
    other_tgv = Leg("Offenburg", "Strasbourg", at(23, 50), at(23, 58), Mode.TRAIN, "SWEG", "RS 1",
                    stops=[stop("Offenburg", 23, 50, start=True), stop("Strasbourg", 23, 58, end=True)])
    idos = Journey(provider="IDS", legs=[night, other_tgv], fare_status=FareStatus.UNKNOWN,
                   raw={"plans": [[{"kind": "offer", "from": [0, 0], "to": [0, 1], "price": "1394",
                                    "system": "IDOS", "name": "jízdenka z e-shopu IDOS"},
                                   {"kind": "buy", "from": [1, 0], "to": [1, 1]}]]})
    better = await _hack(routes=[idos])._improve(query, segments)
    assert ticketplan.total(better) == Decimal(1394 + 1983)
    assert [s.provider for s in better] == ["ČD", "SNCF"]


@pytest.mark.asyncio
async def test_obb_ticket_for_part_of_route_is_used():
    """ÖBB prodá jen München → Zürich (část spoje): i ta jízdenka se nabídne ke skladbě."""
    query = SearchQuery(origin="Plzeň", destination="Curych", departure=at(8))
    a = Leg("Plzeň hl.n.", "München Hbf", at(9, 7), at(13, 18), Mode.TRAIN, "ČD", "EC 360",
            stops=[stop("Plzeň hl.n.", 9, 7, start=True), stop("München Hbf", 13, 18, end=True)])
    b = Leg("München Hbf", "Zürich HB", at(13, 51), at(18, 28), Mode.TRAIN, "ÖBB", "EC 190",
            stops=[stop("München Hbf", 13, 51, start=True), stop("Lindau-Reutin", 16, 41),
                   stop("Zürich HB", 18, 28, end=True)])
    partial = Journey(provider="ÖBB", legs=[a, b], fare_status=FareStatus.NOT_BOOKABLE,
                      raw={"partial": {"from": "München Hbf", "to": "Zürich HB", "eur": "60", "czk": "1500"}})
    segments = [ticket("IDS", [a], 364), ticket("DB regionální (odhad)", [b], 2480)]
    better = await _hack(routes=[partial])._improve(query, segments)
    assert ticketplan.total(better) == Decimal(364 + 1500)
    assert better[-1].provider == "ÖBB"


@pytest.mark.asyncio
async def test_unknown_bus_piece_is_completed_and_result_is_whole():
    """Kus bez prodejce (regionální autobus z plánovače) doplní tarif IDESKA –
    spoj má celou cenu, ne jen „známé jízdenky“."""
    query = SearchQuery(origin="Třeboň", destination="Mariánské Lázně", departure=at(8), passengers=GROUP)
    names = [("Třeboň, Táboritská", 9, 7), ("Štěpánovice", 9, 18), ("Lišov, náměstí Míru", 9, 23),
             ("České Budějovice, aut.nádr.", 9, 45)]
    bus = Leg("Třeboň, Táboritská", "České Budějovice, aut.nádr.", at(9, 7), at(9, 45), Mode.BUS,
              "ČSAD AUTOBUSY České Budějovice a.s.", "Bus 340300 17",
              stops=[stop(n, h, m, start=i == 0, end=i == len(names) - 1) for i, (n, h, m) in enumerate(names)])
    unknown = Journey(provider="?", legs=[bus], fare_status=FareStatus.UNKNOWN)
    train = ticket("Arriva", [r_634(), rj_512()], 1575, "SJT (Systém jednotného tarifu)")
    better = await _hack()._improve(query, [unknown, train])
    assert ticketplan.total(better) is not None
    assert better[0].provider == "IDESKA"


@pytest.mark.asyncio
async def test_abroad_piece_compares_obb_with_idos_eshop_for_single_adult():
    """Do ciziny se e-shopu IDOS (ČD) ptá i tehdy, když tentýž vlak prodá ÖBB –
    ČD bývá výrazně levnější (Praha → Offenburg 1394 Kč místo 7212 Kč)."""
    from vyhledavac.registry import load_plugins

    load_plugins()
    night = Leg("Praha hl.n.", "Offenburg", at(18, 31), at(23, 46), Mode.TRAIN, "České dráhy, a.s.", "EC 458")
    route = Journey(provider="Plánovač", legs=[night],
                    raw={"plans": [[{"kind": "buy", "from": [0, 0], "to": [0, 1]}]]})
    obb = Journey(provider="ÖBB", legs=[night], fare_status=FareStatus.PRICED,
                  fares=[Fare(amount=Decimal(290), currency="EUR", amount_czk=Decimal(7212))])
    cd = Journey(provider="IDS", legs=[night], fare_status=FareStatus.PRICED,
                 fares=[Fare(amount=Decimal(1394), currency="CZK", amount_czk=Decimal(1394), label="IDOS")])
    asked = []

    async def search(q, allow_browser=False, only=None):
        asked.append(tuple(only or ()))
        return [cd] if only == ["IDS"] else [obb]

    query = SearchQuery(origin="Praha", destination="Offenburg", departure=at(18))
    hack = ic.IdosCompleteHack()
    hack._browser_locks = {}
    found = await hack.find(query, [route], search)
    assert ("IDS",) in asked
    assert found and found[0].price == Decimal(1394) and found[0].segments[0].provider == "ČD"

    # skupina: e-shop IDOS cenu nedá – neptat se (zdržel by o 10–25 s)
    asked.clear()
    group = SearchQuery(origin="Praha", destination="Offenburg", departure=at(18), passengers=GROUP)
    hack = ic.IdosCompleteHack()
    hack._browser_locks = {}
    await hack.find(group, [route], search)
    assert ("IDS",) not in asked


def test_multi_span_eshop_ticket_is_never_split_into_free_parts():
    """E-shop IDOS píše cenu jízdenky na víc úseků jen k prvnímu (ostatní 0 Kč):
    jako kus skladby se smí použít jen celá – jinak by vyšla „jízdenka za 0 Kč“."""
    a = Leg("Plzeň hl.n.", "Domažlice", at(9, 10), at(9, 58), Mode.TRAIN, "ČD", "Ex 360",
            stops=[stop("Plzeň hl.n.", 9, 10, start=True), stop("Domažlice", 9, 58, end=True)])
    b = Leg("Domažlice", "Schwandorf", at(10, 5), at(12, 5), Mode.BUS, "alex", "Bus RE25",
            stops=[stop("Domažlice", 10, 5, start=True), stop("Schwandorf", 12, 5, end=True)])
    c = Leg("Schwandorf", "Nürnberg Hbf", at(12, 10), at(13, 20), Mode.TRAIN, "DB Regio", "RE40",
            stops=[stop("Schwandorf", 12, 10, start=True), stop("Nürnberg Hbf", 13, 20, end=True)])
    together = Journey(provider="IDS", legs=[a, b, c], raw={"plans": [[
        {"kind": "offer", "from": [0, 0], "to": [0, 1], "price": "463", "system": "IDOS", "name": "x"},
        {"kind": "offer", "from": [1, 0], "to": [1, 1], "price": "0", "system": "IDOS", "name": "x"},
        {"kind": "buy", "from": [2, 0], "to": [2, 1]}]]})
    offers = ic._plan_offers(together)
    assert [(o.origin, o.destination, o.price) for o in offers] == [("Plzeň hl.n.", "Schwandorf", 463)]
    apart = Journey(provider="IDS", legs=[a, b, c], raw={"plans": [[
        {"kind": "offer", "from": [0, 0], "to": [0, 1], "price": "463", "system": "IDOS", "name": "x"},
        {"kind": "buy", "from": [1, 0], "to": [1, 1]},
        {"kind": "offer", "from": [2, 0], "to": [2, 1], "price": "0", "system": "IDOS", "name": "x"}]]})
    assert ic._plan_offers(apart) == []
    plan, linked = ic._join_offers(apart.raw["plans"][0], [a, b, c])
    assert linked and len(plan) == 3


@pytest.mark.asyncio
async def test_linked_ticket_parts_keep_original_combination():
    query = SearchQuery(origin="Plzeň", destination="Norimberk", departure=at(9))
    a = Leg("Plzeň hl.n.", "Domažlice", at(9, 10), at(9, 58), Mode.TRAIN, "ČD", "Ex 360",
            stops=[stop("Plzeň hl.n.", 9, 10, start=True), stop("Domažlice", 9, 58, end=True)])
    b = Leg("Domažlice", "Nürnberg Hbf", at(10, 5), at(13, 20), Mode.TRAIN, "DB", "RE40",
            stops=[stop("Domažlice", 10, 5, start=True), stop("Nürnberg Hbf", 13, 20, end=True)])
    first = ticket("IDOS", [a], 463)
    first.raw["linked"] = True
    second = ticket("IDOS", [b], 0)
    second.raw["linked"] = True
    same = await ticketplan.improve(query, [first, second])
    assert [s.price for s in same] == [463, 0]


@pytest.mark.asyncio
async def test_variant_after_first_ticket_abroad():
    """Cesta do ciziny bez IDS: první jízdenka (FlixBus do Mnichova) zůstane,
    na zbytek se z odpovědí, které už přišly, nabídne levnější pozdější vlak."""
    query = SearchQuery(origin="Praha", destination="Curych", departure=at(8))
    bus = Leg("Praha, Florenc", "München Hbf", at(8), at(12, 30), Mode.BUS, "FlixBus", "FlixBus N1")
    ec = Leg("München Hbf", "Zürich HB", at(13, 51), at(18, 28), Mode.TRAIN, "ÖBB", "EC 190")
    later = Leg("München Hbf", "Zürich HB", at(14, 51), at(19, 28), Mode.TRAIN, "ÖBB", "EC 194")
    segments = [ticket("FlixBus", [bus], 558), ticket("ÖBB", [ec], 2027)]
    cheap_later = ticket("ÖBB", [later], 900)
    key = (normalize(ic._query_name("München Hbf", True)), normalize(ic._query_name("Zürich HB", True)),
           False, False)
    variants = await _hack({key: [ticket("ÖBB", [ec], 2027), cheap_later]})._variants(query, segments)
    assert [v.price for v in variants] == [Decimal(1458)]
    assert variants[0].segments[0].provider == "FlixBus" and variants[0].arrival == at(19, 28)
