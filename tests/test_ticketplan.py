"""Nejlevnější skladba jízdenek na konkrétní spojení (``vyhledavac.ticketplan``)."""

from decimal import Decimal

import pytest

from vyhledavac import ticketplan
from vyhledavac.models import DiscountCard, Fare, FareStatus, Journey, Leg, Mode, Passenger, SearchQuery, Stop

from conftest import at

#: Smíšená skupina z dotazu uživatele: dva senioři přes 75, dva dospělí, student s ISIC.
GROUP = (Passenger(76), Passenger(78), Passenger(50), Passenger(48),
         Passenger(20, cards=(DiscountCard.ISIC,)))


def stop(name, h, m, end=False, start=False):
    return Stop(name, arrival=None if start else at(h, m), departure=None if end else at(h, m))


def bus_300():
    stops = [stop("Třeboň, aut.nádr.", 9, 5, start=True), stop("Lišov, náměstí Míru", 9, 23),
             stop("České Budějovice, aut.nádr.", 9, 45, end=True)]
    return Leg("Třeboň, aut.nádr.", "České Budějovice, aut.nádr.", at(9, 5), at(9, 45), Mode.BUS,
               "ČSAD AUTOBUSY České Budějovice a.s.", "Bus 300", stops=stops)


def r_634():
    stops = [stop("České Budějovice", 10, 4, start=True), stop("Protivín", 10, 30), stop("Strakonice", 10, 53),
             stop("Horažďovice předměstí", 11, 10), stop("Nepomuk", 11, 25), stop("Plzeň hl.n.", 11, 55, end=True)]
    return Leg("České Budějovice", "Plzeň hl.n.", at(10, 4), at(11, 55), Mode.TRAIN, "České dráhy, a.s.",
               "R 634 Rožmberk", stops=stops)


def sp_1666():
    stops = [stop("Plzeň hl.n.", 13, 8, start=True), stop("Pňovany", 13, 30), stop("Stříbro", 13, 42),
             stop("Planá u Mariánských Lázní", 13, 58), stop("Mariánské Lázně", 14, 14, end=True)]
    return Leg("Plzeň hl.n.", "Mariánské Lázně", at(13, 8), at(14, 14), Mode.TRAIN, "jiný dopravce (SJT)",
               "Sp 1666", stops=stops)


def ic_562():
    stops = [stop("Plzeň hl.n.", 14, 4, start=True), stop("Stříbro", 14, 30),
             stop("Mariánské Lázně", 15, 5, end=True)]
    return Leg("Plzeň hl.n.", "Mariánské Lázně", at(14, 4), at(15, 5), Mode.TRAIN, "jiný dopravce (SJT)",
               "IC 562", stops=stops)


def ticket(provider, legs, price, tariff=""):
    return Journey(provider=provider, legs=legs, fare_status=FareStatus.PRICED,
                   fares=[Fare(amount=Decimal(price), currency="CZK", amount_czk=Decimal(price),
                               label=provider, tariff=tariff)])


def cut(leg, a, b):
    path = ticketplan.Path([leg])
    return path.slice(path.node(0, a), path.node(0, b))


@pytest.mark.asyncio
async def test_ids_network_ticket_continues_on_regional_train_instead_of_sjt():
    """IDESKA do Strakonic + IDPK do Plzně + SJT z Plzně (602 Kč): na spěšný
    vlak z Plzně platí IDPK – síťová IDPK ze Strakonic (240 min) pokryje celý
    zbytek cesty za 294 Kč pro skupinu."""
    query = SearchQuery(origin="Třeboň", destination="Mariánské Lázně", departure=at(8), passengers=GROUP)
    train = r_634()
    segments = [ticket("IDESKA", [bus_300(), *cut(train, 0, 2)], 345),
                ticket("IDPK", cut(train, 2, 5), 294),
                ticket("Arriva", [sp_1666()], 602, "SJT (Systém jednotného tarifu)")]
    better = await ticketplan.improve(query, segments)
    assert ticketplan.total(better) == Decimal(639)
    assert [s.provider for s in better] == ["IDESKA", "IDPK"]
    assert better[-1].origin == "Strakonice" and better[-1].destination == "Mariánské Lázně"


@pytest.mark.asyncio
async def test_long_distance_train_keeps_seller_ticket():
    """IC z Plzně do Mariánských Lázní IDS nebere – jízdenka SJT zůstane."""
    query = SearchQuery(origin="Třeboň", destination="Mariánské Lázně", departure=at(8), passengers=GROUP)
    train = r_634()
    segments = [ticket("IDESKA", [bus_300(), *cut(train, 0, 2)], 345),
                ticket("IDPK", cut(train, 2, 5), 294),
                ticket("Arriva", [ic_562()], 602, "SJT (Systém jednotného tarifu)")]
    same = await ticketplan.improve(query, segments)
    assert [s.provider for s in same] == ["IDESKA", "IDPK", "Arriva"]
    assert ticketplan.total(same) == Decimal(1241)


@pytest.mark.asyncio
async def test_missing_bus_ticket_is_completed_by_ids_tariff():
    """Autobus z plánovače, který žádný e-shop neprodá (cena chybí): tarif IDESKA
    ho ocení – i když jméno zastávky „Štěpánovice“ má jmenovce na Moravě."""
    names = [("Třeboň, Táboritská", 11, 7), ("Štěpánovice, Vranín", 11, 14), ("Štěpánovice", 11, 18),
             ("Lišov, náměstí Míru", 11, 23), ("České Budějovice, aut.nádr.", 11, 45)]
    stops = [stop(n, h, m, start=i == 0, end=i == len(names) - 1) for i, (n, h, m) in enumerate(names)]
    bus = Leg("Třeboň, Táboritská", "České Budějovice, aut.nádr.", at(11, 7), at(11, 45), Mode.BUS,
              "ČSAD AUTOBUSY České Budějovice a.s.", "Bus 340300 17", stops=stops)
    query = SearchQuery(origin="Třeboň", destination="České Budějovice", departure=at(11), passengers=GROUP)
    unknown = Journey(provider="?", legs=[bus], fare_status=FareStatus.UNKNOWN)
    done = await ticketplan.improve(query, [unknown])
    assert [s.provider for s in done] == ["IDESKA"]
    # 2× senior 65+ (50 %), 2× plné, student 50 %: Třeboň (326) → Budějovice (100)
    assert ticketplan.total(done) == Decimal(170)


@pytest.mark.asyncio
async def test_answer_for_part_of_journey_from_memory_is_used():
    """Odpověď prodejce, která už přišla, přesně na kus cesty (tentýž odjezd
    i příjezd) – i bez nového dotazu z ní může být levnější skladba."""
    from vyhledavac.normalize import same_place

    query = SearchQuery(origin="Praha", destination="Linz", departure=at(8))
    a = Leg("Praha hl.n.", "České Budějovice", at(8, 15), at(10, 40), Mode.TRAIN, "ÖBB", "rj 1")
    b = Leg("České Budějovice", "Linz Hbf", at(10, 45), at(12, 50), Mode.TRAIN, "ÖBB", "rj 1")
    whole = ticket("ÖBB", [a, b], 900)
    cheap_head = ticket("RegioJet", [Leg("Praha hl.n.", "České Budějovice", at(8, 14), at(10, 41),
                                          Mode.TRAIN, "ÖBB", "rj 1")], 250)
    cheap_tail = ticket("ÖBB", [Leg("České Budějovice", "Linz Hbf", at(10, 45), at(12, 50),
                                     Mode.TRAIN, "ÖBB", "rj 1")], 400)
    better = await ticketplan.improve(query, [whole], options=[cheap_head, cheap_tail], same_place=same_place)
    assert ticketplan.total(better) == Decimal(650)
    assert [s.provider for s in better] == ["RegioJet", "ÖBB"]


def test_cheapest_prefers_fewer_tickets_on_tie():
    legs = [Leg("A", "B", at(8), at(9), Mode.TRAIN, "X", "1"), Leg("B", "C", at(9, 10), at(10), Mode.TRAIN, "X", "2")]
    path = ticketplan.Path(legs)
    one = ticketplan.Piece(0, path.last, Decimal(100), ticket("X", legs, 100))
    two = [ticketplan.Piece(0, 1, Decimal(50), ticket("Y", legs[:1], 50)),
           ticketplan.Piece(1, path.last, Decimal(50), ticket("Y", legs[1:], 50))]
    assert ticketplan.cheapest(path, [*two, one]) == [one]


@pytest.mark.asyncio
async def test_ids_ticket_chains_systems_and_buys_rest_on_long_distance_train():
    """Spoj ČD s přestupy (osobák, rychlík, IC): IDESKA do Strakonic, IDPK do
    Plzně a na IC do Mariánských Lázní jízdenka SJT – dřív hack uměl jen jeden
    systém + zbytek u ČD (IDESKA do Nepomuku a OneTicket dál za 1304 Kč)."""
    from vyhledavac.hacks.ids_ticket import IdsTicketHack

    os_ = Leg("Třeboň", "Veselí n.Lužnicí", at(14, 58), at(15, 22), Mode.TRAIN, "ČD", "OS 8716",
              stops=[stop("Třeboň", 14, 58, start=True), stop("Veselí n.Lužnicí", 15, 22, end=True)])
    r = Leg("Veselí n.Lužnicí", "Plzeň hl.n.", at(15, 28), at(17, 55), Mode.TRAIN, "ČD", "R 658 Rožmberk",
            stops=[stop("Veselí n.Lužnicí", 15, 28, start=True), stop("České Budějovice", 16, 4),
                   stop("Strakonice", 16, 53), stop("Nepomuk", 17, 25), stop("Plzeň hl.n.", 17, 55, end=True)])
    ic = Leg("Plzeň hl.n.", "Mariánské Lázně", at(18, 4), at(19, 5), Mode.TRAIN, "ČD", "IC 558 Západní expres",
             stops=[stop("Plzeň hl.n.", 18, 4, start=True), stop("Stříbro", 18, 30),
                    stop("Mariánské Lázně", 19, 5, end=True)])
    journey = Journey(provider="OneTicket", legs=[os_, r, ic], fare_status=FareStatus.PRICED,
                      fares=[Fare(amount=Decimal(2002), currency="CZK", amount_czk=Decimal(2002))])
    tail = ticket("Arriva", [Leg("Plzeň hl.n.", "Mariánské Lázně", at(18, 4), at(19, 5), Mode.TRAIN,
                                 "jiný dopravce (SJT)", "IC 558")], 602, "SJT (Systém jednotného tarifu)")
    asked = []

    async def search(q, allow_browser=False, only=None):
        asked.append((q.origin, q.destination))
        return [tail] if q.origin.startswith("Plzeň") else []

    query = SearchQuery(origin="Třeboň", destination="Mariánské Lázně", departure=at(14), passengers=GROUP)
    hack = IdsTicketHack()
    answers = await hack._chain_answers(query, [journey], search)
    found = await hack._chains(query, [journey], [], answers)
    assert ("Plzeň hl.n.", "Mariánské Lázně") in asked
    assert len(found) == 1
    result = found[0]
    assert [s.provider for s in result.segments] == ["IDESKA", "IDPK", "Arriva"]
    assert result.price == Decimal(345 + 294 + 602)
    assert result.legs == journey.legs


# ---- rozklad cesty na uzly ------------------------------------------------------------
def test_path_nodes_merge_transfers_and_slice_cuts_rides():
    path = ticketplan.Path([bus_300(), r_634()])
    # bus: 3 zastávky, vlak: 6 – přestup (aut. nádr. → nádraží) je jeden uzel
    assert len(path.nodes) == 3 + 6 - 1
    transfer = path.node(1, 0)
    assert transfer == path.node(0, -1) and path.transfers() == [transfer]
    assert path.name_to(transfer) == "České Budějovice, aut.nádr."
    assert path.name_from(transfer) == "České Budějovice"
    legs = path.slice(path.node(0, 1), path.node(1, 2))
    assert [(l.origin, l.destination) for l in legs] == [("Lišov, náměstí Míru", "České Budějovice, aut.nádr."),
                                                          ("České Budějovice", "Strakonice")]
    assert legs[1].departure == at(10, 4) and legs[1].arrival == at(10, 53)


def test_reach_of_chain():
    legs = [Leg("A", "B", at(8), at(9), Mode.TRAIN, "X", "1"), Leg("B", "C", at(9, 10), at(10), Mode.TRAIN, "X", "2"),
            Leg("C", "D", at(10, 10), at(11), Mode.TRAIN, "X", "3")]
    path = ticketplan.Path(legs)
    pieces = [ticketplan.Piece(0, 1, Decimal(1), ticket("X", legs[:1], 1)),
              ticketplan.Piece(1, 2, Decimal(1), ticket("X", legs[1:2], 1))]
    assert ticketplan.reach(path, pieces) == (2, 3)


def test_option_must_match_both_ends_in_time_and_place():
    from vyhledavac.normalize import same_place

    legs = [Leg("Praha hl.n.", "Brno hl.n.", at(8), at(10, 30), Mode.TRAIN, "ČD", "R 1")]
    path = ticketplan.Path(legs)
    exact = ticket("RegioJet", [Leg("Praha hl.n.", "Brno hl.n.", at(8, 2), at(10, 34), Mode.TRAIN, "RJ", "R 1")], 199)
    later = ticket("RegioJet", [Leg("Praha hl.n.", "Brno hl.n.", at(8, 30), at(11), Mode.TRAIN, "RJ", "RJ 2")], 99)
    elsewhere = ticket("FlixBus", [Leg("Praha, Florenc", "Jihlava", at(8), at(10, 30), Mode.BUS, "Flix", "N1")], 50)
    pieces = ticketplan.option_pieces(path, [exact, later, elsewhere], same_place)
    assert [(p.start, p.end, p.price) for p in pieces] == [(0, 1, 199)]


# ---- kde jízdenka IDS platí ----------------------------------------------------------------
def test_ride_systems_by_line_and_train_category():
    from vyhledavac.ticketplan import ride_systems

    assert ride_systems(Leg("A", "B", at(8), at(9), Mode.BUS, "FlixBus", "FlixBus N123")) == set()
    assert ride_systems(Leg("A", "B", at(8), at(9), Mode.BUS, "ČSAD", "Bus 340300 17")) == {"IDESKA"}
    assert ride_systems(Leg("A", "B", at(8), at(9), Mode.TRAIN, "České dráhy, a.s.", "Os 8708")) is None
    assert ride_systems(Leg("A", "B", at(8), at(9), Mode.TRAIN, "jiný dopravce (SJT)", "Sp 1666")) is None
    assert ride_systems(Leg("A", "B", at(8), at(9), Mode.TRAIN, "ČD", "IC 562 Západní expres")) == {"IDESKA"}
    assert ride_systems(Leg("A", "B", at(8), at(9), Mode.TRAIN, "ČD", "rj 512")) == set()
    assert ride_systems(Leg("A", "B", at(8), at(9), Mode.TRAIN, "RegioJet", "RJ 1000")) == set()
    # rozpis IDOS uznal IDESKA i na autobusu bez čísla licence
    assert ride_systems(Leg("A", "B", at(8), at(9), Mode.BUS, "ČSAD", "Bus 300"), {"IDESKA"}) == {"IDESKA"}


@pytest.mark.asyncio
async def test_ids_ticket_must_fit_its_validity():
    """Relační jízdenka IDESKA Třeboň → Strakonice platí 180 minut, síťová na
    5 hodin 300 minut: s pětihodinovým čekáním na vlak zbude jen Jihočeská 24."""
    query = SearchQuery(origin="Třeboň", destination="Strakonice", departure=at(8))
    names = [("České Budějovice", 14, 4), ("Protivín", 14, 30), ("Strakonice", 14, 53)]
    late = Leg("České Budějovice", "Strakonice", at(14, 4), at(14, 53), Mode.TRAIN, "České dráhy, a.s.",
               "R 642", stops=[stop(n, h, m, start=i == 0, end=i == 2) for i, (n, h, m) in enumerate(names)])
    path = ticketplan.Path([bus_300(), late])
    pieces = await ticketplan.ids_pieces(query, path, [{"IDESKA"}, set()])
    whole = min((p for p in pieces if p.start == 0 and p.end == path.last), key=lambda p: p.price)
    assert "Jihočeská 24" in whole.segment.fares[0].tariff and whole.price == Decimal(195)
    quick = ticketplan.Path([bus_300(), *cut(r_634(), 0, 2)])        # navazující vlak v 10:04
    pieces = await ticketplan.ids_pieces(query, quick, [{"IDESKA"}, set()])
    whole = min((p for p in pieces if p.start == 0 and p.end == quick.last), key=lambda p: p.price)
    assert whole.price == Decimal(99)                                  # relační, plné jízdné


@pytest.mark.asyncio
async def test_ids_tariff_for_group_counts_each_passenger():
    """Dospělý plné jízdné (49 Kč), dítě 10 let polovic (24 Kč), dítě do 6 let zdarma."""
    query = SearchQuery(origin="Třeboň", destination="České Budějovice", departure=at(8),
                        passengers=(Passenger(40), Passenger(10), Passenger(4)))
    path = ticketplan.Path([bus_300()])
    pieces = await ticketplan.ids_pieces(query, path, [{"IDESKA"}])
    whole = next(p for p in pieces if p.start == 0 and p.end == path.last)
    assert whole.price == Decimal(73)


@pytest.mark.asyncio
async def test_jizni_expres_ideska_only_inside_south_bohemia():
    query = SearchQuery(origin="České Budějovice", destination="Praha", departure=at(8))
    ec = Leg("České Budějovice", "Praha hl.n.", at(13, 58), at(15, 39), Mode.TRAIN, "České dráhy, a.s.",
             "EC 334 Jižní expres",
             stops=[stop("České Budějovice", 13, 58, start=True), stop("Veselí nad Lužnicí", 14, 20),
                    stop("Tábor", 14, 37), stop("Benešov u Prahy", 15, 5), stop("Praha hl.n.", 15, 39, end=True)])
    path = ticketplan.Path([ec])
    pieces = await ticketplan.ids_pieces(query, path)
    ideska = {(path.name_from(p.start), path.name_to(p.end)) for p in pieces if p.segment.provider == "IDESKA"}
    assert ("České Budějovice", "Tábor") in ideska
    assert all(end not in ("Benešov u Prahy", "Praha hl.n.") for _, end in ideska)
    assert not any(p.segment.provider == "PID" for p in pieces)       # PID na EC neplatí


def test_foreign_station_is_not_taken_for_town_stop():
    """„Linz Hbf“ se nesmí dohledat jako zastávka v Třeboni (zóna IDESKA Třeboně)."""
    from vyhledavac.providers.ids_idos import _place

    found = _place("Linz Hbf", True, set(), ("Třeboň",))
    assert found is None or found.code == "AT"
    stop_in_town = _place("Táboritská", False, set(), ("Třeboň",))
    assert stop_in_town is not None and stop_in_town.name == "Třeboň, Táboritská"
