"""Kategorie cestujícího a její překlad na tarify dopravců.

Kódy RegioJetu a FlixBusu jsou ověřené proti živým API (září 2026).
"""

import pytest

from vyhledavac.cache import MemoryCache
from vyhledavac.models import DiscountCard, FareCategory, Passenger
from vyhledavac.providers.flixbus import FlixBusProvider
from vyhledavac.providers.oneticket import OneTicketProvider
from vyhledavac.providers.regiojet import RegioJetProvider

ISIC = (DiscountCard.ISIC,)


@pytest.mark.parametrize("passenger, category", [
    (Passenger(age=3), FareCategory.INFANT),
    (Passenger(age=10), FareCategory.CHILD),
    (Passenger(age=16, cards=ISIC), FareCategory.YOUTH),
    (Passenger(age=22), FareCategory.ADULT),            # bez průkazu není student
    (Passenger(age=22, cards=ISIC), FareCategory.STUDENT),
    (Passenger(age=26, cards=ISIC), FareCategory.ADULT),    # studentská sleva jen do 26
    (Passenger(age=70, cards=ISIC), FareCategory.SENIOR),
    (Passenger(age=40), FareCategory.ADULT),
])
def test_category(passenger, category):
    assert passenger.category is category


@pytest.mark.parametrize("passenger, code", [
    (Passenger(age=3), "ATTENDED_CHILD"),
    (Passenger(age=10), "CHILD_UNDER_12"),
    (Passenger(age=14), "CZECH_STUDENT_PASS_15"),
    (Passenger(age=16), "CZECH_STUDENT_PASS_15"),
    (Passenger(age=21, cards=ISIC), "CZECH_STUDENT_PASS_26"),
    (Passenger(age=70), "ISIC"),       # u RegioJetu je "ISIC" senior 65+
    (Passenger(age=40), "REGULAR"),
])
def test_regiojet_tariff(passenger, code):
    assert RegioJetProvider(cache=MemoryCache())._tariff(passenger) == code


def test_regiojet_isic_student_is_not_sent_as_senior():
    # Regrese: dřív ISIC → kód "ISIC", který RegioJet chápe jako seniora.
    assert RegioJetProvider(cache=MemoryCache())._tariff(Passenger(age=22, cards=ISIC)) != "ISIC"


@pytest.mark.parametrize("passengers, products", [
    ((Passenger(age=40),), {"adult": 1}),
    ((Passenger(age=22, cards=ISIC),), {"student": 1}),     # dřív "adult" → bez slevy
    ((Passenger(age=70),), {"senior": 1}),
    ((Passenger(age=40), Passenger(age=10)), {"adult": 1, "children_6_17": 1}),
    ((Passenger(age=40), Passenger(age=3)), {"adult": 1, "children_0_5": 1}),
])
def test_flixbus_products(passengers, products):
    assert FlixBusProvider(cache=MemoryCache())._passenger_products(passengers) == products


def test_flixbus_kids_alone_have_no_price():
    assert FlixBusProvider._needs_escort({"children_6_17": 1})
    assert not FlixBusProvider._needs_escort({"student": 1, "children_6_17": 1})


@pytest.mark.parametrize("passenger, key", [
    (Passenger(age=21, cards=ISIC), "STUDENT_26_ISIC"),
    (Passenger(age=16), "UNDER_18"),
    (Passenger(age=70), "SENIOR"),
    (Passenger(age=40), "ADULT"),
])
def test_oneticket_fare(passenger, key):
    assert OneTicketProvider(cache=MemoryCache())._fare_key(passenger) == key


@pytest.mark.parametrize("passenger, label", [
    (Passenger(age=21, cards=ISIC), "Junior 18—25 let, Žákovský průkaz ČR"),  # případ z praxe
    (Passenger(age=21), "Junior 18—25 let"),
    (Passenger(age=16), "Junior 15—17 let"),
    (Passenger(age=8), "Dítě 6—9 let"),
    (Passenger(age=12), "Dítě 10—14 let"),
    (Passenger(age=70), "Dospělý 65 let a starší"),
    (Passenger(age=40), "Dospělý 26—64 let"),
])
def test_cd_tariff(passenger, label):
    from vyhledavac.providers.cd import _cd_tariff, _tariff_label

    assert _tariff_label(passenger) == label


def test_cd_patterns_match_real_button_titles():
    """Titulky tlačítek zkopírované z cd.cz (září 2026)."""
    import re
    from vyhledavac.providers.cd import _cd_tariff

    titles = [
        "Přidat cestujícího Dospělý 65 let a starší",
        "Přidat cestujícího Dospělý 26—64 let, IN 25",
        "Přidat cestujícího Junior 18—25 let, Žákovský průkaz ČR",
        "Přidat cestujícího Junior 18—25 let",
        "Přidat cestujícího Junior 15—17 let",
        "Přidat cestujícího Dítě 10—14 let",
        "Přidat cestujícího Dítě 6—9 let",
        "Přidat cestujícího Dítě 0—5 let",
    ]
    for age, cards, expected in [(21, ISIC, 2), (21, (), 3), (16, (), 4), (12, (), 5),
                                 (8, (), 6), (3, (), 7), (70, (), 0)]:
        _, pattern = _cd_tariff(Passenger(age=age, cards=cards))
        hits = [i for i, t in enumerate(titles) if re.search(pattern, t)]
        assert hits == [expected], (age, cards, hits)
