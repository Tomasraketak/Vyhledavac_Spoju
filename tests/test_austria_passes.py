"""Rakouské síťové jízdenky na den (Freizeit-Tickety, VOR, Tirol 2Plus, VMOBIL)."""

import datetime as dt
from decimal import Decimal

import pytest

from vyhledavac import geo
from vyhledavac.hacks.cd_europe import RegionalPassHack
from vyhledavac.models import DEFAULT_TZ, Journey, Leg, Mode, Passenger, SearchQuery

from conftest import make_journey

SAT, SUN, TUE, HOLIDAY = 24, 25, 20, 26          # říjen 2026; 26. 10. = rakouský státní svátek


def at(hour, minute=0, day=SAT):
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=DEFAULT_TZ)


def pass_named(name):
    return next(p for p in geo.PASSES if p.name == name)


def adults(n):
    return tuple(Passenger(age=30) for _ in range(n))


# ---- zeměpis a kalendář ---------------------------------------------------------------
@pytest.mark.parametrize("name, state", [
    ("Wien Hbf", "W"), ("Flughafen Wien", "N"), ("Wiener Neustadt Hbf", "N"), ("Gmünd NÖ", "N"),
    ("Neusiedl am See", "B"), ("Linz/Donau Hbf", "O"), ("Summerau", "O"), ("Salzburg Hbf", "S"),
    ("Graz Hbf", "ST"), ("Bruck/Mur", "ST"), ("Selzthal", "ST"), ("Klagenfurt Hbf", "K"),
    ("Gmünd in Kärnten", "K"), ("Feldkirchen in Kärnten", "K"), ("Innsbruck Hbf", "T"), ("Lienz", "T"),
    ("Feldkirch", "V"), ("Bregenz", "V"), ("Třeboň", None), ("Brno hl.n.", None), ("Baden-Baden", None)])
def test_state_of(name, state):
    assert geo.state_of(name) == state


def test_country_of_knows_small_austrian_stations():
    assert geo.country_of("Leoben Hbf") == "AT" and geo.country_of("Kitzbühel") == "AT"
    assert geo.country_of("Ostrava, Mor.Ostrava, Horn.poliklinika") != "AT"


def test_austrian_holidays():
    days = geo.holidays(2026, "AT")
    assert dt.date(2026, 4, 6) in days            # Velikonoční pondělí (Velikonoce 5. 4. 2026)
    assert dt.date(2026, 6, 4) in days            # Boží tělo
    assert dt.date(2026, 10, 26) in days          # Nationalfeiertag
    assert dt.date(2026, 10, 28) not in days      # český svátek, rakouský ne
    assert geo.is_day_off(dt.date(2026, 10, 24), "AT") and not geo.is_day_off(dt.date(2026, 10, 20), "AT")


# ---- ceny a platnost -----------------------------------------------------------------
def test_prices_by_group():
    family = [Passenger(age=40), Passenger(age=38), Passenger(age=10), Passenger(age=8)]
    ooe, tirol = pass_named("Freizeit-Ticket OÖ"), pass_named("Tagesticket Tirol 2Plus")
    assert ooe.price(family) == Decimal("26.50") and ooe.price(adults(1)) == Decimal("26.50")
    assert ooe.price(adults(3)) is None                                       # nejvýš 2 dospělí
    assert tirol.price([*adults(2), *(Passenger(age=9) for _ in range(4))]) is None   # nejvýš 3 děti
    assert tirol.price([*adults(2), Passenger(age=3), *(Passenger(age=9) for _ in range(3))]) \
        == Decimal("42.60")                                                   # do 6 let se nepočítá
    karnten = pass_named("Freizeit-Ticket Kärnten")
    assert karnten.price([Passenger(age=40), Passenger(age=12)]) == Decimal("22.80")
    assert karnten.price(adults(2)) == Decimal("29.80")
    assert pass_named("Freizeit-Ticket Steiermark").price(adults(3)) == Decimal(45)
    assert pass_named("VOR Freizeitticket").price(adults(2)) == Decimal("39.80")
    assert pass_named("VOR Freizeitticket Plus").price(adults(1)) == Decimal("29.60")
    assert pass_named("VMOBIL Tageskarte MAXIMO").price(adults(1)) == Decimal("18.40")
    assert pass_named("Freizeit-Ticket Salzburg").price(family) == Decimal(25)


def test_validity_days_and_hours():
    stmk, ooe = pass_named("Freizeit-Ticket Steiermark"), pass_named("Freizeit-Ticket OÖ")
    salzburg, era = pass_named("Freizeit-Ticket Salzburg"), geo.PASSES[0]
    assert stmk.valid_at(at(6)) and stmk.valid_at(at(6, day=HOLIDAY)) and not stmk.valid_at(at(12, day=TUE))
    assert ooe.valid_at(at(9, day=TUE)) and not ooe.valid_at(at(8, 30, TUE)) and ooe.valid_at(at(7))
    assert era.valid_at(at(7, day=HOLIDAY))                                   # svátek = celý den
    assert salzburg.valid_at(at(6, day=TUE))                                  # každý den
    # Štýrsko do půlnoci, Korutany do 3:00 dalšího dne, VMOBIL 24 h
    assert not stmk.valid_until(at(22), at(0, 30, SUN))
    assert pass_named("Freizeit-Ticket Kärnten").valid_until(at(22), at(0, 30, SUN))
    vmobil = pass_named("VMOBIL Tageskarte MAXIMO")
    assert vmobil.valid_until(at(18), at(17, day=SUN)) and not vmobil.valid_until(at(18), at(19, day=SUN))


def test_area_and_trains():
    stmk = pass_named("Freizeit-Ticket Steiermark")
    rex = [Leg("Graz Hbf", "Leoben Hbf", at(8), at(9), Mode.TRAIN, "ÖBB", "REX 1")]
    assert stmk.covers_legs(rex)
    railjet = [Leg("Graz Hbf", "Leoben Hbf", at(8), at(9), Mode.TRAIN, "ÖBB", "RJX 552")]
    assert not stmk.covers_legs(railjet)
    bus = [Leg("Graz Hbf", "Weiz", at(8), at(9), Mode.BUS, "ÖBB", "Bus 200")]
    assert stmk.covers_legs(bus) and not geo.PASSES[0].covers_legs(bus)        # ERT bez autobusů
    to_linz = [Leg("Graz Hbf", "Linz/Donau Hbf", at(8), at(12), Mode.TRAIN, "ÖBB", "REX 20")]
    assert not stmk.covers_legs(to_linz) and geo.PASSES[0].covers_legs(to_linz)
    vor = pass_named("VOR Freizeitticket")
    to_wien = [Leg("Retz", "Wien Hbf", at(8), at(9), Mode.TRAIN, "ÖBB", "REX 4")]
    assert not vor.covers_legs(to_wien) and pass_named("VOR Freizeitticket Plus").covers_legs(to_wien)


# ---- hack: cesta celá v Rakousku ----------------------------------------------------------
@pytest.mark.asyncio
async def test_weekend_trip_in_styria_with_freizeit_ticket():
    query = SearchQuery(origin="Graz Hbf", destination="Leoben Hbf", departure=at(7),
                        passengers=adults(2))
    rex = make_journey(provider="ÖBB", origin="Graz Hbf", destination="Leoben Hbf", dep=at(8),
                       arr=at(9, 10), price=1300, line="REX 1",
                       stops=["Graz Hbf", "Bruck/Mur", "Leoben Hbf"])
    railjet = make_journey(provider="ÖBB", origin="Graz Hbf", destination="Leoben Hbf", dep=at(8, 30),
                           arr=at(9, 20), price=1400, line="RJX 552")

    async def search(q, allow_browser=False, only=None):
        raise AssertionError("cesta po Rakousku – nic se nedoptává")

    found = await RegionalPassHack().find(query, [rex, railjet], search)
    assert len(found) == 1
    hacked = found[0]
    assert hacked.departure == rex.departure and hacked.provider == "Freizeit-Ticket Steiermark"
    assert hacked.price == Decimal("759.00")                       # 2 × 15 € po 25,30 Kč
    assert "Štýrsko" in hacked.notes[1] and "soboty" in hacked.notes[1]
    # ve všední den Steiermark neplatí (a jiná jízdenka pro Graz → Leoben se nevyplatí)
    weekday = SearchQuery(origin="Graz Hbf", destination="Leoben Hbf", departure=at(7, day=TUE),
                          passengers=adults(2))
    rex_tue = make_journey(provider="ÖBB", origin="Graz Hbf", destination="Leoben Hbf", dep=at(8, day=TUE),
                           arr=at(9, 10, TUE), price=1300, line="REX 1")
    tue = await RegionalPassHack().find(weekday, [rex_tue], search)
    assert all(j.provider == "Einfach-Raus-Ticket" for j in tue)          # jen ERT (38 €) – ne Štýrsko


@pytest.mark.asyncio
async def test_carinthia_family_on_national_holiday():
    query = SearchQuery(origin="Klagenfurt Hbf", destination="Villach Hbf", departure=at(9, day=HOLIDAY),
                        passengers=(Passenger(age=40), Passenger(age=11)))
    s_bahn = make_journey(provider="ÖBB", origin="Klagenfurt Hbf", destination="Villach Hbf",
                          dep=at(10, day=HOLIDAY), arr=at(10, 35, HOLIDAY), price=900, line="S 1")

    async def search(q, allow_browser=False, only=None):
        return []

    found = await RegionalPassHack().find(query, [s_bahn], search)
    karnten = [j for j in found if j.provider == "Freizeit-Ticket Kärnten"]
    assert karnten and karnten[0].price == Decimal("576.84")           # 14,90 + 7,90 €


@pytest.mark.asyncio
async def test_not_offered_when_obb_ticket_is_cheaper():
    query = SearchQuery(origin="Innsbruck Hbf", destination="Kufstein", departure=at(7),
                        passengers=adults(1))
    cheap = make_journey(provider="ÖBB", origin="Innsbruck Hbf", destination="Kufstein", dep=at(8),
                         arr=at(9), price=300, line="REX 1")

    async def search(q, allow_browser=False, only=None):
        return []

    assert await RegionalPassHack().find(query, [cheap], search) == []


# ---- hack: z Česka přes hranici --------------------------------------------------------------
@pytest.mark.asyncio
async def test_from_znojmo_to_vienna_with_vor_plus():
    query = SearchQuery(origin="Znojmo", destination="Wien Hbf", departure=at(6), passengers=adults(1))
    legs = [Leg("Znojmo", "Retz", at(7), at(7, 20), Mode.TRAIN, "ČD", "Os 4800"),
            Leg("Retz", "Wien Hbf", at(7, 30), at(8, 40), Mode.TRAIN, "ÖBB", "REX 4")]
    through = Journey(provider="ÖBB", legs=legs, fares=[], raw={})
    direct = make_journey(provider="ČD", origin="Znojmo", destination="Wien Hbf", dep=at(7),
                          arr=at(8, 40), price=1200, line="Os 4800")
    head = make_journey(provider="ČD", origin="Znojmo", destination="Retz", dep=at(7), arr=at(7, 20),
                        price=60, line="Os 4800")
    asked = []

    async def search(q, allow_browser=False, only=None):
        asked.append((q.destination, tuple(only or ())))
        return [head] if q.destination == "Retz" else []

    found = await RegionalPassHack().find(query, [through, direct], search)
    assert asked == [("Retz", ("ČD",))]                           # jedna vstupní stanice, jeden dotaz
    best = min(found, key=lambda j: j.price)
    assert "VOR Freizeitticket Plus" in best.provider               # bez Vídně by nedojel
    assert best.price == Decimal(60) + Decimal("748.88")            # 29,60 € po 25,30 Kč
