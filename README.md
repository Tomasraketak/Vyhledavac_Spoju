# Vyhledávač spojů

Metavyhledávač vlaků a autobusů v Evropě – „Skyscanner pro pozemní dopravu“.
Hledá napříč dopravci, porovnává ceny a umí najít levnější varianty pomocí
tzv. travel hacků (lomené jízdenky, skryté město).

Verze 2 je přepsaná od základu: původní scrapery se nahradily tam, kde to
šlo, přímým voláním API, a všechno je rozdělené do zásuvných modulů.

---

## Proč přepis

Původní kód padal na třech věcech:

| Problém | Příčina | Řešení ve verzi 2 |
|---|---|---|
| Antibot ochrana blokovala scrapery | Vše jelo přes prohlížeč na veřejném webu | 3 ze 4 dopravců volají přímo JSON API, které používá jejich vlastní web – žádný prohlížeč |
| Scrapery se rozbíjely | Natvrdo zadané CSS selektory | API místo DOMu; kde prohlížeč zbyl, jsou selektory vrstvené a selhání je hlášené |
| Špatné časy u nočních spojů | Čas jako `"HH:MM"` a počítání `% 1440` | Všude skutečné `datetime` s časovou zónou |
| Chyby mizely beze stopy | `except Exception: pass` | Každý dopravce vrací `ProviderResult` s chybou; UI ji ukáže |
| Přidat dopravce znamenalo zásah na 5 místech | `ADAPTERS` slovník + `if/elif` větve | Registr pluginů – nový dopravce je jeden soubor |

---

## Instalace

```bash
pip install -r requirements.txt          # jen httpx – stačí pro 3 dopravce
pip install playwright && playwright install chromium   # volitelně, pro ČD
```

Python 3.10+.

## Použití

```bash
# příkazová řádka
python -m vyhledavac Praha Brno
python -m vyhledavac Praha Vídeň --date 01.10.2026 --time 08:00 --isic --sort price
python -m vyhledavac Praha Vídeň --hacks split_ticket --max-price 800 --pareto
python -m vyhledavac --list-providers
python -m vyhledavac Praha Brno --json        # strojově čitelný výstup

# grafické rozhraní
python -m vyhledavac.gui
```

Z Pythonu:

```python
import asyncio, datetime as dt
from vyhledavac import SearchQuery, Passenger, DiscountCard
from vyhledavac.engine import SearchEngine

async def main():
    engine = SearchEngine.build(
        ["RegioJet", "FlixBus", "ÖBB"],
        filter_spec=[("departure_window", {"hours": 8}), "deduplicate", "pareto"],
        hack_names=["split_ticket"],
    )
    query = SearchQuery(
        origin="Praha", destination="Vídeň",
        departure=dt.datetime(2026, 10, 1, 8, 0),
        passengers=(Passenger(age=20, cards=(DiscountCard.ISIC,)),),
    )
    result = await engine.search(query, sort_by="price")
    for journey in result.journeys:
        print(journey.departure, journey.price_label, journey.provider)
    await engine.aclose()

asyncio.run(main())
```

---

## Stav dopravců

Ověřeno živým během v září 2026:

| Dopravce | Způsob | Ceny | Zastávky | Stav |
|---|---|---|---|---|
| **RegioJet** | JSON API (`brn-ybus-pubapi.sa.cz`) | ✅ | ✅ úseky | **ověřeno** – 28 spojů Praha→Brno za ~4 s |
| **FlixBus** | JSON API (`global.api.flixbus.com`) | ✅ | částečně | **ověřeno** – 45 spojů Praha→Brno za ~2 s |
| **ÖBB** | HAFAS (`fahrplan.oebb.at`) | ❌ jen jízdní řád | ✅ **všechny mezizastávky** | **ověřeno** – vrací i české vlaky |
| **ČD** | prohlížeč (Playwright) | ✅ | částečně | **neověřeno** – viz níže |

### Poznámka k ČD

ČD nemají veřejné API – formulář posílá POST zpět na stejnou stránku.
Zůstal tedy prohlížeč. Při ověřování se ukázalo, že **selektory z původního
kódu už na webu ČD neexistují** (`.suggestion-options` se nenajde), takže
původní scraper vracel prázdný seznam, aniž by to dal najevo. Nová verze má
selektorů několik generací a když žádný nesedí, vyhodí srozumitelnou chybu
místo tichého selhání.

Celé vyhledávání ČD se mi z tohoto prostředí nepodařilo projet do konce, takže
ho berte jako **neověřený** – pravděpodobně bude potřeba selektory doladit
podle aktuálního webu. Soubor `vyhledavac/providers/cd.py` má seznamy
selektorů pohromadě nahoře, úprava je tak otázka jednoho místa.

Dobrá zpráva: české vlaky včetně kompletní trasy vrací i **ÖBB přes HAFAS**,
takže aplikace není bez ČD slepá – chybí jen ceny ČD.

### ZSSK a OneTicket

Jejich původní skripty (`ZSSK_Fetch.py`, `OpenTicketFetch.py`) nebyly mezi
zaslanými soubory, takže nejsou přepsané. ZSSK API (`ikvc.slovakrail.sk`)
nebylo z tohoto prostředí dostupné. Přidání je ale otázka jednoho souboru –
viz `examples/novy_dopravce.py`.

---

## Jak je to poskládané

```
vyhledavac/
├── models.py        datový model (datetime, Decimal – žádné "HH:MM" a inf)
├── http.py          odolný async HTTP klient (retry, detekce blokace, limity)
├── browser.py       stealth Playwright – jen pro dopravce bez API
├── cache.py         SQLite cache s TTL (nahradila CSV soubory)
├── money.py         kurzy ČNB
├── normalize.py     porovnávání názvů stanic (diakritika, aliasy)
├── registry.py      registr pluginů
├── engine.py        orchestrace, izolace chyb, průběžné výsledky
├── providers/       dopravci      (regiojet, flixbus, obb, cd)
├── filters/         filtry        (11 vestavěných)
└── hacks/           travel hacky  (split_ticket, hidden_city)
```

### Jak se bráníme blokaci

1. **Nejlepší obrana je nescrapovat.** Weby dopravců samy volají JSON API –
   voláme ho taky. Na API se antibot ochrana typicky nevztahuje.
2. **Konzistentní otisk.** Hlavičky se posílají jako celá sada
   (`User-Agent` + `sec-ch-ua` + `Sec-Fetch-*`), ne jen `User-Agent`.
   Nesourodá kombinace je snadno odhalitelná.
3. **Slušné tempo.** Rozestupy mezi požadavky na stejný host a strop souběhu.
4. **Rozpoznání blokace.** Cloudflare/captcha stránka se pozná i když přijde
   s HTTP 200; klient pak vymění identitu místo opakování téhož.
5. **Backoff s jitterem** a respektování `Retry-After`.
6. Kde prohlížeč zbyl, maskuje `navigator.webdriver`, pluginy, WebGL i
   viditelnost karty a neblokuje se na uspávání záložek na pozadí.

---

## Rozšiřování

### Nový dopravce

Jeden soubor ve `vyhledavac/providers/`:

```python
@PROVIDERS.register()
class MujDopravce(HttpProvider):
    name = "MujDopravce"

    async def search(self, query):
        data = await self.client.get_json("https://api.../spojeni", params={...})
        return [Journey(provider=self.name, legs=[...], fares=[...])]
```

Registr ho najde sám. Kompletní ukázka: `examples/novy_dopravce.py`.
Externí plugin mimo balíček jde načíst přes `VYHLEDAVAC_PLUGINS`:

```bash
VYHLEDAVAC_PLUGINS=muj_modul python -m vyhledavac Praha Brno
```

### Nový filtr

```python
@FILTERS.register()
class PouzeDenniSpoje(Filter):
    name = "pouze_denni"

    def apply(self, journeys, query):
        return [j for j in journeys if 6 <= j.departure.hour < 22]
```

Viz `examples/vlastni_filtr.py`.

### Nový travel hack

```python
@HACKS.register()
class MujHack(TravelHack):
    name = "muj_hack"

    async def find(self, query, journeys, search):
        dilci = await search(query.leg_query("Praha", "Břeclav"))
        ...
```

`search` je funkce, kterou si hack doptá libovolný dílčí úsek; engine
výsledky cachuje, takže se stejný dotaz neposílá dvakrát.

---

## Travel hacky

**`split_ticket` – lomená jízdenka.** Zůstanete v jednom vlaku, ale koupíte
dvě jízdenky (A→X a X→B). U přeshraničních cest to často vyjde levněji.
Body lomu se hledají ve **skutečné trase vlaku** (ze seznamu zastávek), ne
jen v pevném seznamu – hraniční stanice mají jen přednost. Body, na které se
nedá koupit jízdenka (státní hranice), se přeskakují.

**`hidden_city` – skryté město.** Jízdenka do vzdálenější stanice může být
levnější; vystoupíte dřív. Nabízí se jen když vlak ve vašem cíli skutečně
staví a nejde o spoj s povinnou místenkou. Výsledek vždy nese varování –
zbytek jízdenky propadá a někteří dopravci to v podmínkách zakazují.

Obě strategie nabídnou variantu jen při úspoře nad nastavený práh
(výchozí 3 %, u skrytého města 8 %).

---

## Testy

```bash
pip install pytest pytest-asyncio
python -m pytest -q
```

69 testů. Pokrývají mimo jiné konkrétní chyby původní verze: spoje přes
půlnoc, `limit` aplikovaný před řazením, dva různé vlaky považované za
jeden, nebo chybějící cena vydávaná za nekonečnou.

## Licence a fair play

Nástroj je pro osobní použití. Dotazy jsou omezené tempem, aby dopravcům
nezatěžovaly servery. Ceny a dostupnost vždy ověřte u dopravce –
před koupí se mohou lišit.
